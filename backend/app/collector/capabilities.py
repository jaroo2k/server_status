from app.collector.sources.logfile import LOG_FILES

# Prints one capability token per line. Hardware tools are detected now so the
# hardware collectors (next phase) can be enabled per server.
DETECT_SCRIPT = (
    "for c in journalctl dmesg systemctl smartctl ipmitool sensors ras-mc-ctl mdadm; do "
    "command -v $c >/dev/null 2>&1 && echo $c; done; "
    + "".join(f"[ -r {p} ] && echo file:{p}; " for p in LOG_FILES)
    + "[ -r /proc/mdstat ] && echo mdstat; "
    "[ -d /sys/devices/system/edac/mc ] && echo edac; "
    "true"
)


def parse_capabilities(output: str) -> list[str]:
    return sorted({line.strip() for line in output.splitlines() if line.strip()})
