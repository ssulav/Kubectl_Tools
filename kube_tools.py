#!/usr/bin/python3

import os
import sys
import logging
import time
import subprocess

from configparser import ConfigParser
from string import Template


# These are the sequences need to get colored output
RESET_SEQ = "\033[0m"
RED_COLOR_SEQ = "\033[0;31m"
YELLOW_COLOR_SEQ = "\033[0;33m"
GREEN_COLOR_SEQ = "\033[0;32m"

logging.addLevelName(logging.INFO, GREEN_COLOR_SEQ + logging.getLevelName(logging.INFO))
logging.addLevelName(logging.WARNING, YELLOW_COLOR_SEQ + logging.getLevelName(logging.WARNING))
logging.addLevelName(logging.ERROR, RED_COLOR_SEQ + logging.getLevelName(logging.ERROR))
logging.basicConfig(stream=sys.stdout, level=logging.INFO, format='%(levelname)-1s: %(message)s' + RESET_SEQ)

logger = logging.getLogger()

CONF_FILENAME = 'ktoolrc.ini'
DEFAULT_CONF = {
    'container': {
        'podname': '',
        'namespace': os.getenv('USER')
    },
    'mapping': {
        'dst_package_dir': '/usr/local/lib/python3.6/dist-packages',
        'dst_test_dir': '/hwqe/hadoopqe',
        'dst_yaml_dir': '/ansible'
    },
    'command': {
        'kcp': "/usr/local/bin/kubectl cp $src_path ${namespace}/${podname}:${dest_path}",
        'kexec': "/usr/local/bin/kubectl exec -t $podname -n $namespace -c system-test -- $command",
        'kpf': "/usr/local/bin/kubectl port-forward pod/$podname -n $namespace $debug_port:$debug_port --address 127.0.0.1",
        'sudo_login_and_run': "sudo su - hrt_qa -c \"$run_command\" ",
        'login_and_run': "su -c \"$run_command\" ",
        'texas_entry': "pkill supervisord && texas_test_entrypoint --test-type system_test"
                       " --run-tests-path /ansible/system_test.yml",
        'ansible_play': "ansible-playbook $yaml_file",
        'cd_and_run': "source /etc/profile && cd $test_dir && $test_command",
        'pytest': "python3 -m pytest -s $test_file_path --output=artifacts_${test_name} 2>&1"
                  " | tee /tmp/console_${test_name}.log",
        'pytest_debug': "python3 -m debugpy --wait-for-client --listen 0.0.0.0:$debug_port -m pytest -s $test_file_path"
                        " --output=artifacts_${test_name} 2>&1 | tee /tmp/console_${test_name}.log",
        'debug_port': '5678'
    }
}


class KubectlTools:
    def __init__(self):
        self.file_path = None
        self.project_name = None
        self.ktoolrc_file = None
        self.cur_config = ConfigParser()
        self.dest_path = None

        self.__check_and_validate_parameters()
        self.__read_and_validate_config_file()
        self.__map_src_to_dest_path()

    def __get_command(self, command_key):
        if self.cur_config.has_option('command', command_key):
            return self.cur_config.get('command', command_key)
        default_value = DEFAULT_CONF.get('command', {}).get(command_key)
        if default_value:
            logger.warning("Command '%s' missing in %s; using default.", command_key, self.ktoolrc_file)
            return default_value
        logger.error("Command '%s' not found in config and no default available.", command_key)
        sys.exit(1)

    def __check_and_validate_parameters(self):
        len_arg = len(sys.argv[1:])

        # Checking for Required Argument FilePath
        if len_arg >= 1 and sys.argv[1]:
            # Normalize to absolute path for reliability across callers
            self.file_path = sys.argv[1]
            if not os.path.isabs(self.file_path):
                self.file_path = os.path.abspath(self.file_path)
            logger.info("file_path = %s", self.file_path)
        else:
            logger.error("$FilePath$ is missing from Arguments")
            self.__print_usage_and_exit()

        # Derive project name from repository when possible, or use provided argument
        inferred_project = self.__infer_project_name_from_path(self.file_path)
        provided_project = None
        if len_arg >= 2 and sys.argv[2]:
            provided_project = sys.argv[2]
            self.project_name = provided_project
            logger.info("project_name (provided) = %s", self.project_name)
        else:
            self.project_name = inferred_project
            logger.info("project_name (inferred) = %s", self.project_name)

        # If provided project name doesn't match the file's repository, prefer inferred
        if provided_project and inferred_project and provided_project != inferred_project:
            if provided_project not in self.file_path:
                logger.warning("Provided project_name '%s' does not match file path repo '%s'. Using '%s'.",
                               provided_project, inferred_project, inferred_project)
                self.project_name = inferred_project

        # Checking for config file ktoolrc & creating if not exist
        if len_arg >= 3 and sys.argv[3]:
            self.ktoolrc_file = sys.argv[3]
            logger.warning("Using config file from %s", self.ktoolrc_file)
        else:
            # Prefer config colocated with this tool (Kubectl_Tools project)
            script_dir = os.path.dirname(os.path.abspath(__file__))
            candidate_paths = []
            # 1) Kubectl_Tools dir
            candidate_paths.append(os.path.join(script_dir, CONF_FILENAME))
            # 2) Git repo root of the file being acted on
            git_root = self.__find_git_root(os.path.dirname(self.file_path))
            if git_root:
                candidate_paths.append(os.path.join(git_root, CONF_FILENAME))
            # 3) Current working directory
            candidate_paths.append(os.path.join(os.getcwd(), CONF_FILENAME))

            self.ktoolrc_file = next((p for p in candidate_paths if os.path.isfile(p)), None)
            if self.ktoolrc_file:
                logger.warning("Using config file from %s", self.ktoolrc_file)
            else:
                # Default: create in Kubectl_Tools directory
                self.ktoolrc_file = os.path.join(script_dir, CONF_FILENAME)
                logger.warning("No config file found! Writing default to %s", self.ktoolrc_file)
                self.__write_conf_to_file()

    @staticmethod
    def __find_git_root(start_dir):
        try:
            result = subprocess.run([
                "git", "-C", start_dir, "rev-parse", "--show-toplevel"
            ], capture_output=True, text=True, check=True)
            git_root = result.stdout.strip()
            return git_root if git_root else None
        except Exception:
            return None

    def __infer_project_name_from_path(self, abs_file_path):
        """Infer the repository name (top-level folder) for the given file path.

        Priority:
          1) git rev-parse --show-toplevel
          2) Detect after 'github' segment in the absolute path
          3) Known repository names present in the path
        """
        # 1) Try git
        try:
            file_dir = os.path.dirname(abs_file_path)
            result = subprocess.run([
                "git", "-C", file_dir, "rev-parse", "--show-toplevel"
            ], capture_output=True, text=True, check=True)
            git_root = result.stdout.strip()
            if git_root:
                return os.path.basename(git_root)
        except Exception:
            pass

        # 2) Try to locate after 'github' segment (e.g., /.../github/QE/ozone-qe/..)
        parts = os.path.normpath(abs_file_path).split(os.sep)
        try:
            idx = parts.index("github")
            if idx + 2 < len(parts):
                return parts[idx + 2]
        except ValueError:
            pass

        # 3) Fallback: check for known repo names in the path
        known_repos = [
            "beaver-qe", "beaver-common", "ozone-qe", "Kubectl_Tools"
        ]
        for repo in known_repos:
            if repo in abs_file_path:
                return repo

        # Last resort: top-most folder under the user's workspace path
        return parts[0] if parts else ""

    def __read_and_validate_config_file(self):
        self.cur_config.optionxform = str
        if os.path.isfile(self.ktoolrc_file):
            self.cur_config.read(self.ktoolrc_file)
        else:
            logger.error("Give Config File [%s] doesn't exists!", self.ktoolrc_file)
            sys.exit(1)
        # Checking for podname
        st_podname = self.cur_config.get('container', 'podname')
        namespace = self.cur_config.get('container', 'namespace')
        if st_podname:
            logger.warning("Using podname = <%s>!\n\t--> If you want to change the podname, change it at <%s> file <--",
                           st_podname, self.ktoolrc_file)
        else:
            logger.error("Please set the value for 'podname' in the file %s", self.ktoolrc_file)
            sys.exit(1)

    def __map_src_to_dest_path(self):
        if self.project_name == "texas_test_entrypoint":  # self.file_path.endswith(('.yaml', '.yml'))
            from pathlib import Path
            target_path = self.file_path[self.file_path.index(self.project_name) + len(self.project_name + "/files"):]
            logger.info(self.file_path)
            self.dest_path = os.path.join(self.cur_config.get(section='mapping', option='dst_yaml_dir'),
                                          target_path)
        else:
            self.dest_path = self.cur_config.get(
                section='mapping',
                option='dst_package_dir' if self.project_name in ['beaver-qe', 'beaver-common'] else 'dst_test_dir'
            ) + self.file_path.split(self.project_name)[-1]

    # Helper Functions
    def __write_conf_to_file(self):
        config_parser = ConfigParser()
        config_parser.optionxform = str
        for sec, prop_dict in DEFAULT_CONF.items():
            config_parser.add_section(sec)
            for k, v in prop_dict.items():
                config_parser.set(sec, k, v)
        try:
            with open(self.ktoolrc_file, 'w+', encoding='utf-8') as configfile:
                config_parser.write(configfile)
        except IOError as e:
            logger.error(e)
            logger.error("Unable to write the config to config file %s", self.ktoolrc_file)
        return True

    @staticmethod
    def __print_usage_and_exit():
        print(f"{YELLOW_COLOR_SEQ} {sys.argv[0]} <$FilePath$> [<$ProjectName$>] [ktoolrc_filepath] {RESET_SEQ}")
        sys.exit(-1)

    @staticmethod
    def __run_command(command):
        if os.system(command) == 0:
            logger.info("Command Ran Successfully")
            return True
        else:
            logger.error("Command Execution Failed")
            return False

    # Exposed Functions
    def kubectl_copy_to_container(self):
        if os.path.exists(self.file_path) and self.project_name in self.file_path:
            st_podname = self.cur_config.get('container', 'podname')
            namespace = self.cur_config.get('container', 'namespace')

            dest_path = self.dest_path
            if os.path.isdir(self.file_path):
                dest_path = os.path.dirname(self.dest_path)

            # Ensure parent directory exists on container before copying
            remote_dir_to_create = dest_path if os.path.isdir(self.file_path) else os.path.dirname(dest_path)
            mkdir_cmd = f"mkdir -p {remote_dir_to_create}"
            kexec_mkdir = Template(self.cur_config.get('command', 'kexec')).substitute(
                podname=st_podname,
                namespace=namespace,
                command=mkdir_cmd
            )
            logger.info("[Running] cmd = %s", kexec_mkdir)
            self.__run_command(kexec_mkdir)

            cmd_template = Template(self.cur_config.get('command', 'kcp'))
            kcp_command = cmd_template.substitute(src_path=self.file_path, namespace=namespace,
                                                  podname=st_podname, dest_path=dest_path)

            logger.info("[Running] cmd = %s", kcp_command)
            return self.__run_command(kcp_command)
        else:
            logger.warning("Please check if [%s] exists & is part of project [%s]", self.file_path, self.project_name)
            return False

    def kubectl_run_test_on_container(self):
        st_podname = self.cur_config.get('container', 'podname')
        namespace = self.cur_config.get('container', 'namespace')

        if self.file_path.endswith(('.yaml', '.yml')):
            if os.path.basename(self.file_path) == 'system_test.yml':
                cmd = self.cur_config.get('command', 'texas_entry')
            else:
                cmd = Template(self.cur_config.get('command', 'ansible_play')).substitute(yaml_file=self.dest_path)
            login_run_cmd = Template(self.cur_config.get('command', 'login_and_run')).substitute(run_command=cmd)
        else:
            # Assuming Pytest Run on File or Folder
            test_dir = self.cur_config.get('mapping', 'dst_test_dir')
            relative_path = os.path.relpath(self.dest_path, test_dir)
            test_name = os.path.basename(self.dest_path).split('.')[0] + '_' + str(int(time.time()))

            pytest_cmd = Template(self.cur_config.get('command', 'pytest')).substitute(test_file_path=relative_path,
                                                                                       test_name=test_name)
            cmd = Template(self.cur_config.get('command', 'cd_and_run')).substitute(test_dir=test_dir,
                                                                                    test_command=pytest_cmd)
            login_run_cmd = Template(self.cur_config.get('command', 'sudo_login_and_run')).substitute(run_command=cmd)
        kexec_cmd = Template(self.cur_config.get('command', 'kexec')).substitute(podname=st_podname,
                                                                                 namespace=namespace,
                                                                                 command=login_run_cmd)

        logger.info("[Running] cmd = %s", kexec_cmd)
        return self.__run_command(kexec_cmd)

    def kubectl_debug_test_on_container(self):
        """Run tests under debugpy, waiting for an external debugger to attach on port 5678.

        To debug: port-forward 5678 from the pod to localhost, then attach VS Code to localhost:5678.
        """
        st_podname = self.cur_config.get('container', 'podname')
        namespace = self.cur_config.get('container', 'namespace')

        if self.file_path.endswith(('.yaml', '.yml')):
            logger.error("Debug mode is intended for Python tests, not YAML playbooks.")
            return False
        # Start port-forward locally in background
        debug_port = Template(self.__get_command('debug_port')).substitute()
        logger.info("[Running - DEBUG] debug_port: %s", debug_port)
        debug_port = int(debug_port)
        # Free up local and remote debug ports/processes from earlier runs
        local_pkill_pf = f"pkill -f 'kubectl port-forward .* {debug_port}:{debug_port}' || true"
        logger.info("[Running - DEBUG] cleanup local port-forward: %s", local_pkill_pf)
        self.__run_command(local_pkill_pf)

        remote_pkill_debug = "pkill -f debugpy || pkill -f 'python3 -m debugpy' || true"
        kexec_remote_pkill = Template(self.cur_config.get('command', 'kexec')).substitute(
            podname=st_podname,
            namespace=namespace,
            command=remote_pkill_debug
        )
        logger.info("[Running - DEBUG] cleanup remote debugpy: %s", kexec_remote_pkill)
        self.__run_command(kexec_remote_pkill)

        # Ensure debugpy is available in the container
        install_cmd = "python3 -m pip install -q --user debugpy || true"
        login_install_cmd = Template(self.cur_config.get('command', 'sudo_login_and_run')).substitute(
            run_command=install_cmd
        )
        kexec_install_cmd = Template(self.cur_config.get('command', 'kexec')).substitute(
            podname=st_podname,
            namespace=namespace,
            command=login_install_cmd
        )
        logger.info("[Running - DEBUG] ensure debugpy: %s", kexec_install_cmd)
        self.__run_command(kexec_install_cmd)


        pf_cmd = Template(self.__get_command('kpf')).substitute(
            podname=st_podname,
            namespace=namespace,
            debug_port=debug_port
        )
        pf_bg_cmd = f"nohup {pf_cmd} > /tmp/kpf_{st_podname}_{debug_port}.log 2>&1 &"
        logger.info("[Running - DEBUG] port-forward: %s", pf_bg_cmd)
        self.__run_command(pf_bg_cmd)

        logger.info("Running in Debug mode.")
        test_dir = self.cur_config.get('mapping', 'dst_test_dir')
        relative_path = os.path.relpath(self.dest_path, test_dir)
        test_name = os.path.basename(self.dest_path).split('.')[0] + '_' + str(int(time.time()))

        pytest_cmd = Template(self.__get_command('pytest_debug')).substitute(
            test_file_path=relative_path,
            test_name=test_name,
            debug_port=debug_port
        )
        cmd = Template(self.cur_config.get('command', 'cd_and_run')).substitute(
            test_dir=test_dir,
            test_command=pytest_cmd
        )
        login_run_cmd = Template(self.cur_config.get('command', 'sudo_login_and_run')).substitute(
            run_command=cmd
        )
        kexec_cmd = Template(self.cur_config.get('command', 'kexec')).substitute(
            podname=st_podname,
            namespace=namespace,
            command=login_run_cmd
        )

        logger.info("[Running - DEBUG] cmd = %s", kexec_cmd)
        logger.warning("Debug mode: ensure 'debugpy' is installed in container and port-forward %s.", debug_port)
        try:
            return self.__run_command(kexec_cmd)
        finally:
            # Stop local port-forward started earlier
            stop_pf_cmd = f"pkill -f 'kubectl port-forward .* {debug_port}:{debug_port}' || true"
            logger.info("[Running - DEBUG] stop port-forward: %s", stop_pf_cmd)
            self.__run_command(stop_pf_cmd)


if __name__ == '__main__':
    kube_tool = KubectlTools()
    if kube_tool.kubectl_copy_to_container():
        kube_tool.kubectl_run_test_on_container()
    else:
        time.sleep(1)
        logger.warning("Skipping KubeTool Run Test as Copy failed")
