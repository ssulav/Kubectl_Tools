"""Resolve Texas pods via kubectl liveness and ozone-live-clusters pool API."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from getpass import getuser
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent
PRIMARY_CONTAINERS = frozenset({"system-test", "systest"})
REPO_KUBECONFIG = REPO_ROOT / "secrets" / "config-rke"
HOME_KUBECONFIG = Path.home() / ".kube" / "config-rke"
DEFAULT_KUBECONFIG = str(REPO_KUBECONFIG)
DEFAULT_LIVE_CLUSTERS_URL = "http://10.240.67.203:8083"
DEFAULT_POOL_SLOT_IDS = [1, 2]
DEFAULT_RESERVED_BY = "ozone-functional-team"
KUBECTL_BIN = "/usr/local/bin/kubectl"


class NoPoolAvailableError(RuntimeError):
    """Raised when no live pool slot can be reserved."""


@dataclass(frozen=True)
class PodTarget:
    podname: str
    namespace: str
    source: str
    slot_id: Optional[int] = None
    reserved_by: Optional[str] = None


def expand_path(path: str) -> str:
    return os.path.expanduser((path or "").strip())


def resolve_kubeconfig(path: Optional[str] = None) -> str:
    """Prefer repo-local secrets/config-rke, then configured path, then ~/.kube/config-rke."""
    candidates: List[Path] = []

    if path:
        configured = Path(expand_path(path))
        if not configured.is_absolute():
            configured = (REPO_ROOT / configured).resolve()
        candidates.append(configured)

    candidates.extend([REPO_KUBECONFIG, HOME_KUBECONFIG])

    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return str(candidate)

    return str((candidates[0] if candidates else REPO_KUBECONFIG).resolve())


def parse_slot_ids(raw: str, default: Optional[List[int]] = None) -> List[int]:
    if raw in (None, ""):
        return list(default or DEFAULT_POOL_SLOT_IDS)
    slot_ids: List[int] = []
    for part in str(raw).split(","):
        part = part.strip()
        if part:
            slot_ids.append(int(part))
    return slot_ids or list(default or DEFAULT_POOL_SLOT_IDS)


def is_truthy(value: str, default: bool = True) -> bool:
    if value in (None, ""):
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def resolve_reserved_by(configured: str = "") -> str:
    value = (configured or "").strip()
    if value:
        return value
    for env_key in ("KTOOL_RESERVED_BY", "USER", "LOGNAME"):
        env_value = (os.getenv(env_key) or "").strip()
        if env_value:
            return env_value
    return getuser()


def _live_clusters_request(
    method: str,
    api_url: str,
    path: str,
    *,
    payload: Optional[Dict] = None,
    timeout: float = 10.0,
) -> Tuple[int, Dict]:
    base_url = (api_url or DEFAULT_LIVE_CLUSTERS_URL).rstrip("/")
    url = "{0}{1}".format(base_url, path)
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.load(response)
            if not isinstance(body, dict):
                return response.status, {}
            return response.status, body
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(body_text)
        except json.JSONDecodeError:
            body = {"error": body_text or exc.reason}
        if not isinstance(body, dict):
            body = {"error": str(body)}
        return exc.code, body
    except urllib.error.URLError as exc:
        raise RuntimeError("live clusters API unreachable: {0}".format(exc.reason)) from exc


def _reservation_payload_from_cluster_response(payload: Dict, slot_id: int) -> Dict:
    cluster = payload.get("cluster") or {}
    if not cluster.get("pod_name"):
        return {"reserved": False, "slot_id": slot_id, "message": "Pod is not available for slot {0}".format(slot_id)}
    return {
        "reserved": True,
        "slot_id": slot_id,
        "cluster": cluster,
        "ktoolrc": payload.get("ktoolrc") or {},
    }


def _reserve_live_pool_slot_legacy(
    api_url: str,
    slot_ids: List[int],
    reserved_by: str,
    *,
    timeout: float = 10.0,
) -> Dict:
    """Fallback for servers without POST /api/clusters/reserve."""
    attempts: List[Dict[str, str]] = []
    pods = fetch_live_pool_pods(api_url, slot_ids, live_only=True, timeout=timeout)
    by_slot = {int(pod["slot_id"]): pod for pod in pods if pod.get("slot_id") is not None}
    default_team = DEFAULT_RESERVED_BY

    for slot_id in slot_ids:
        pod = by_slot.get(int(slot_id))
        if not pod:
            attempts.append({"slot_id": str(slot_id), "reason": "no live pod"})
            continue
        current = (pod.get("reserved_by") or default_team).strip()
        if current != default_team and current != reserved_by.strip():
            attempts.append({"slot_id": str(slot_id), "reason": "already reserved by {0}".format(current)})
            continue
        if current == reserved_by.strip():
            result = {
                "reserved": True,
                "slot_id": slot_id,
                "cluster": {
                    "slot_id": slot_id,
                    "pod_name": pod.get("pod_name"),
                    "pod_namespace": pod.get("pod_namespace"),
                    "cm_url": pod.get("cm_url"),
                    "reserved_by": current,
                },
                "ktoolrc": {
                    "container": {
                        "podname": pod.get("pod_name"),
                        "namespace": pod.get("pod_namespace"),
                        "cluster": pod.get("cm_url"),
                    }
                },
            }
            result["attempts"] = attempts
            return result

        status, payload = _live_clusters_request(
            "POST",
            api_url,
            "/api/clusters/{0}/reserve".format(slot_id),
            payload={"reserved_by": reserved_by},
            timeout=timeout,
        )
        if status == 200:
            result = _reservation_payload_from_cluster_response(payload, slot_id)
            if result.get("reserved"):
                result["attempts"] = attempts
                return result
            attempts.append({"slot_id": str(slot_id), "reason": result.get("message", "reserve failed")})
            continue
        attempts.append(
            {
                "slot_id": str(slot_id),
                "reason": str(payload.get("error") or "HTTP {0}".format(status)),
            }
        )

    return {
        "reserved": False,
        "slot_ids": slot_ids,
        "reserved_by": reserved_by,
        "default_reserved_by": default_team,
        "attempts": attempts,
        "message": "No primary pool slot available with default reservation ({0})".format(default_team),
    }


def reserve_live_pool_slot(
    api_url: str,
    slot_ids: List[int],
    reserved_by: str,
    *,
    timeout: float = 10.0,
) -> Dict:
    """Reserve the first available primary pool slot via POST /api/clusters/reserve."""
    status, payload = _live_clusters_request(
        "POST",
        api_url,
        "/api/clusters/reserve",
        payload={
            "reserved_by": reserved_by,
            "slot_ids": slot_ids,
        },
        timeout=timeout,
    )
    if status == 404:
        logger.info("Bulk reserve API unavailable; falling back to per-slot reserve")
        return _reserve_live_pool_slot_legacy(api_url, slot_ids, reserved_by, timeout=timeout)
    if status >= 500:
        raise RuntimeError(
            "live clusters reserve failed: HTTP {0} {1}".format(status, payload.get("error", payload))
        )
    if status == 409 or not payload.get("reserved"):
        return payload
    return payload


def release_live_pool_slot(
    api_url: str,
    slot_id: int,
    reserved_by: str,
    *,
    timeout: float = 10.0,
) -> None:
    """Release a pool reservation via POST /api/clusters/<slot_id>/release."""
    status, payload = _live_clusters_request(
        "POST",
        api_url,
        "/api/clusters/{0}/release".format(slot_id),
        payload={"reserved_by": reserved_by},
        timeout=timeout,
    )
    if status == 404:
        status, payload = _live_clusters_request(
            "POST",
            api_url,
            "/api/slots/{0}/reserved-by".format(slot_id),
            payload={"reserved_by": DEFAULT_RESERVED_BY},
            timeout=timeout,
        )
    if status >= 400:
        logger.warning(
            "Failed to release live pool slot %s for %s: HTTP %s %s",
            slot_id,
            reserved_by,
            status,
            payload.get("error", payload),
        )
    else:
        logger.info(
            "Released live pool slot %s (reservation reset to %s)",
            slot_id,
            DEFAULT_RESERVED_BY,
        )


def is_pod_live(podname: str, namespace: str, kubeconfig: str) -> Tuple[bool, str]:
    """Return whether the Texas pod is running and its primary container is usable."""
    name = (podname or "").strip()
    ns = (namespace or "").strip()
    if not name or not ns:
        return False, "pod name or namespace missing"

    kubeconfig_path = resolve_kubeconfig(kubeconfig)
    if not os.path.isfile(kubeconfig_path):
        return False, "kubeconfig not found: {0}".format(kubeconfig_path)

    command = [
        KUBECTL_BIN,
        "--kubeconfig",
        kubeconfig_path,
        "get",
        "pod",
        name,
        "-n",
        ns,
        "-o",
        "json",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)

    if result.returncode != 0:
        message = (result.stderr or result.stdout or "kubectl get pod failed").strip()
        if "NotFound" in message or "not found" in message.lower():
            return False, "pod not found"
        return False, message

    try:
        pod = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return False, "invalid kubectl json output: {0}".format(exc)

    phase = ((pod.get("status") or {}).get("phase") or "").strip()
    if phase != "Running":
        return False, "pod phase is {0}".format(phase or "unknown")

    return _evaluate_pod_ready(pod.get("status") or {})


def _evaluate_pod_ready(status: Dict) -> Tuple[bool, str]:
    for condition in status.get("conditions") or []:
        if condition.get("type") == "Ready" and condition.get("status") == "True":
            return True, ""

    for container in status.get("containerStatuses") or []:
        name = (container.get("name") or "").strip()
        if name not in PRIMARY_CONTAINERS:
            continue
        if container.get("ready"):
            return True, ""
        state = container.get("state") or {}
        if state.get("running"):
            return True, ""

    return False, "pod not ready"


def fetch_live_pool_pods(
    api_url: str,
    slot_ids: List[int],
    *,
    live_only: bool = True,
    timeout: float = 10.0,
) -> List[Dict]:
    """Query ozone-live-clusters primary pool pods by slot id."""
    base_url = (api_url or DEFAULT_LIVE_CLUSTERS_URL).rstrip("/")
    query = urllib.parse.urlencode(
        {
            "slot_ids": ",".join(str(slot_id) for slot_id in slot_ids),
            "live_only": "1" if live_only else "0",
        }
    )
    url = "{0}/api/pool-pods?{1}".format(base_url, query)
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("live clusters API HTTP {0}: {1}".format(exc.code, body)) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("live clusters API unreachable: {0}".format(exc.reason)) from exc

    pods = payload.get("pods")
    if not isinstance(pods, list):
        return []
    return [pod for pod in pods if isinstance(pod, dict)]


def pick_live_pool_pod(pods: List[Dict], preferred_slot_ids: List[int]) -> Optional[Dict]:
    by_slot = {
        int(pod["slot_id"]): pod
        for pod in pods
        if pod.get("slot_id") is not None and pod.get("pod_name") and pod.get("pod_namespace")
    }
    for slot_id in preferred_slot_ids:
        candidate = by_slot.get(int(slot_id))
        if candidate:
            return candidate
    return next(iter(by_slot.values()), None)


def _pod_from_reservation_payload(payload: Dict) -> Tuple[str, str, int]:
    cluster = payload.get("cluster") or {}
    ktoolrc = payload.get("ktoolrc") or {}
    container = ktoolrc.get("container") or {}
    pod_name = container.get("podname") or cluster.get("pod_name")
    namespace = container.get("namespace") or cluster.get("pod_namespace")
    slot_id = payload.get("slot_id") or cluster.get("slot_id")
    if not pod_name or not namespace or slot_id is None:
        raise RuntimeError("live clusters reserve response missing pod target")
    return str(pod_name), str(namespace), int(slot_id)


def resolve_pod_target(
    configured_podname: str,
    configured_namespace: str,
    *,
    kubeconfig: str,
    live_clusters_url: str,
    pool_slot_ids: List[int],
    live_clusters_enabled: bool = True,
    liveness_check_enabled: bool = True,
    auto_reserve: bool = True,
    reserved_by: str = "",
) -> PodTarget:
    """Resolve pod target; reserve a live pool slot when falling back to live-clusters."""
    podname = (configured_podname or "").strip()
    namespace = (configured_namespace or "").strip()
    kubeconfig_path = resolve_kubeconfig(kubeconfig)
    reservation_owner = resolve_reserved_by(reserved_by)

    if liveness_check_enabled and podname and namespace:
        live, reason = is_pod_live(podname, namespace, kubeconfig_path)
        if live:
            logger.info(
                "Using configured pod %s/%s from ktoolrc.ini (kubectl liveness ok)",
                namespace,
                podname,
            )
            return PodTarget(podname, namespace, "ktoolrc.ini")
        logger.warning(
            "Configured pod %s/%s is not live (%s)",
            namespace,
            podname,
            reason or "unknown",
        )
    elif podname and namespace:
        logger.info("Using configured pod %s/%s from ktoolrc.ini", namespace, podname)
        return PodTarget(podname, namespace, "ktoolrc.ini")

    if not live_clusters_enabled:
        if podname and namespace:
            return PodTarget(podname, namespace, "ktoolrc.ini (unverified)")
        raise RuntimeError("No pod configured in ktoolrc.ini and live cluster lookup disabled")

    logger.info(
        "Reserving live cluster primary pool slots %s (reserved_by=%s)",
        ",".join(str(slot_id) for slot_id in pool_slot_ids),
        reservation_owner,
    )

    if auto_reserve:
        reservation = reserve_live_pool_slot(
            live_clusters_url,
            pool_slot_ids,
            reservation_owner,
        )
        if reservation.get("reserved"):
            pod_name, pod_namespace, slot_id = _pod_from_reservation_payload(reservation)
            logger.warning(
                "Reserved live cluster pool slot %s pod %s/%s for %s",
                slot_id,
                pod_namespace,
                pod_name,
                reservation_owner,
            )
            return PodTarget(
                pod_name,
                pod_namespace,
                "live-clusters slot {0}".format(slot_id),
                slot_id=slot_id,
                reserved_by=reservation_owner,
            )

        attempts = reservation.get("attempts") or []
        attempt_summary = "; ".join(
            "slot {0}: {1}".format(item.get("slot_id"), item.get("reason"))
            for item in attempts
            if isinstance(item, dict)
        )
        message = reservation.get("message") or "No live pool slot available"
        if attempt_summary:
            message = "{0} ({1})".format(message, attempt_summary)
        raise NoPoolAvailableError(message)

    logger.info(
        "Querying live clusters for primary pool slots %s without reservation",
        ",".join(str(slot_id) for slot_id in pool_slot_ids),
    )
    pods = fetch_live_pool_pods(
        live_clusters_url,
        pool_slot_ids,
        live_only=True,
    )
    selected = pick_live_pool_pod(pods, pool_slot_ids)
    if selected:
        logger.warning(
            "Falling back to live cluster pool slot %s pod %s/%s (unreserved)",
            selected.get("slot_id"),
            selected.get("pod_namespace"),
            selected.get("pod_name"),
        )
        return PodTarget(
            str(selected["pod_name"]),
            str(selected["pod_namespace"]),
            "live-clusters slot {0} (unreserved)".format(selected.get("slot_id")),
            slot_id=int(selected["slot_id"]) if selected.get("slot_id") is not None else None,
        )

    if podname and namespace:
        logger.warning(
            "No live primary pool pods found for slot ids %s; keeping configured pod %s/%s",
            pool_slot_ids,
            namespace,
            podname,
        )
        return PodTarget(podname, namespace, "ktoolrc.ini (fallback)")

    raise NoPoolAvailableError(
        "No live primary pool pods found for slot ids {0}".format(pool_slot_ids)
    )
