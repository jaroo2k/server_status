#!/bin/sh
set -e
install -d -o monitor -g monitor -m 700 /home/monitor/.ssh
install -o monitor -g monitor -m 600 /authorized_keys /home/monitor/.ssh/authorized_keys
ssh-keygen -A
exec /usr/sbin/sshd -D -e
