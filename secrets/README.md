# Kubernetes credentials

Kubectl_Tools uses `secrets/config-rke` for all `kubectl` commands (copy, exec, port-forward, pod liveness checks).

This file is **not** in git. Download it from the internal wiki and save it locally.

## Setup

[Get Started on QE KubeConfig & KubectlTools](https://cloudera.atlassian.net/wiki/spaces/ENG/pages/10963091578/Get+Started+on+QE+KubeConfig+KubectlTools)

```bash
mkdir -p secrets
# save the downloaded kubeconfig as secrets/config-rke
chmod 600 secrets/config-rke
```

## Resolution order

1. `kubernetes.kubeconfig` in `ktoolrc.ini` (paths relative to this repo)
2. `secrets/config-rke` in this repository (local file)
3. `~/.kube/config-rke` (legacy fallback)

## Security

- Do not commit `secrets/config-rke`.
- Rotate the token if it is exposed outside the QE team.
