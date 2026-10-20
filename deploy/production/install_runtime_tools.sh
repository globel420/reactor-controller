#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "install_runtime_tools.sh must run as root" >&2
    exit 1
fi

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

install -d -o root -g root -m 0755 /usr/local/libexec /usr/local/sbin
install -d -o root -g root -m 0755 /etc/systemd/system
install -o root -g root -m 0755 "${script_dir}/bin/reactorctl" /usr/local/sbin/reactorctl
install -o root -g root -m 0755 "${script_dir}/lib/reactor_runtime.py" /usr/local/libexec/reactor_runtime.py

for unit in \
    reactor-network-mode.service \
    reactor-network-monitor.service \
    reactor-network-handoff.service \
    reactor-system-manager.service \
    reactor-preflight.service \
    reactor-stack.service \
    reactor-stack-monitor.service \
    reactor-report-refresh.service; do
    install -o root -g root -m 0644 "${script_dir}/systemd/${unit}" "/etc/systemd/system/${unit}"
done

systemctl daemon-reload
systemctl enable \
    reactor-network-mode.service \
    reactor-network-monitor.service \
    reactor-system-manager.service \
    reactor-preflight.service \
    reactor-stack.service \
    reactor-stack-monitor.service \
    reactor-report-refresh.service >/dev/null

# Bootstrap invokes reactorctl boot before the first machine reboot.  Start the
# host bridge now so the commissioning supervisor has its real buzzer/admin
# endpoint; the service is also enabled for every later boot.
systemctl restart reactor-system-manager.service

echo "Installed Reactor production runtime helpers and boot services."
