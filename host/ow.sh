#!/bin/sh
# One-shot SSH exec against the running OpenWrt box.
# v24 factory LAN is 192.168.10.1; root password is the factory default.
SSH_ASKPASS="$(dirname "$0")/askpass.bat" SSH_ASKPASS_REQUIRE=force DISPLAY=:0 \
exec ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o PreferredAuthentications=password -o NumberOfPasswordPrompts=1 \
  -o ConnectTimeout=8 root@${OWRT_HOST:-192.168.10.1} "$@"
