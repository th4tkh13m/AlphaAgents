"""Resolve exclusive local emulator endpoints for each candidate lease."""


def local_device_ports(devices, config):
    if not devices:
        raise ValueError("Artemis local runner requires assigned ADB devices")
    mapping = config.get("androidworld_device_ports")
    if mapping is None:
        if len(devices) != 1:
            raise ValueError("Multiple local devices require a per-device port mapping")
        mapping = {
            devices[0]: {key: config.get(key) for key in ("console_port", "grpc_port")}
        }
    if not isinstance(mapping, dict) or set(mapping) != set(devices):
        raise ValueError("Port mapping must match the assigned ADB device IDs exactly")
    used = set()
    result = {}
    for device in devices:
        ports = mapping[device]
        if not isinstance(ports, dict):
            raise ValueError(f"Invalid port mapping for {device}")
        console, grpc = ports.get("console_port"), ports.get("grpc_port")
        if (
            not isinstance(console, int)
            or isinstance(console, bool)
            or console % 2
            or not 5554 <= console <= 5682
        ):
            raise ValueError(f"Invalid emulator console port for {device}")
        if (
            not isinstance(grpc, int)
            or isinstance(grpc, bool)
            or not 1024 <= grpc <= 65535
        ):
            raise ValueError(f"Invalid gRPC port for {device}")
        if device != f"emulator-{console}":
            raise ValueError(
                f"ADB serial {device} does not match console port {console}"
            )
        assigned = {console, console + 1, grpc}
        if len(assigned) != 3 or used & assigned:
            raise ValueError(
                "Console, ADB, and gRPC ports must not overlap across devices"
            )
        used.update(assigned)
        result[device] = {"console_port": console, "grpc_port": grpc}
    return result
