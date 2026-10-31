#!/bin/sh
# SUDO_ASKPASS helper for machine-setup's unattended upgrade (sd:6312).
#
# sudo runs it with its prompt as the one argument and reads the password
# from its stdout. It prints the password of the login-Keychain item
# machine-setup-sudo, account `id -un`, which the operator creates on each
# machine (README: Unattended sudo); without the item it prints nothing and
# exits with security's code, so sudo refuses as it would with no terminal.
# machine-setup.sh sets it only for the casks that need sudo (sudo_casks).
set -eu

case "${1:-}" in
  -h|--help|help)
    echo "usage: sudo-askpass.sh PROMPT   (sudo runs it through SUDO_ASKPASS)"
    echo "Print the password of the login-Keychain item machine-setup-sudo."
    exit 0 ;;
  "")
    echo "usage: sudo-askpass.sh PROMPT   (sudo runs it through SUDO_ASKPASS)" >&2
    exit 1 ;;
esac

# The override exists for the tests, which never read a real Keychain.
exec "${MACHINE_SETUP_SECURITY:-security}" find-generic-password \
  -a "$(id -un)" -s machine-setup-sudo -w
