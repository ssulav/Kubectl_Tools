#!/usr/bin/python3

from kube_tools import KubectlTools


# This script will run the python test <$File/folder$> in CONTAINER under debugpy
if __name__ == '__main__':
    kube_tool = KubectlTools()
    kube_tool.kubectl_debug_test_on_container()


