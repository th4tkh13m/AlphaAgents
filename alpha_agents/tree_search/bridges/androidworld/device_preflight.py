"""Check task app installation before attributing outcomes to the agent."""


def check_task_apps(registry, tasks, device, activity_for_app, package_from_activity):
    required = {
        app: package_from_activity(activity_for_app(app))
        for task in tasks
        for app in registry[task].app_names
    }
    installed = set(device.list_packages())
    missing = {
        app: package for app, package in required.items() if package not in installed
    }
    if missing:
        raise RuntimeError(
            f"Missing required benchmark apps: {missing}. Run official AndroidWorld "
            "emulator setup before evaluating; missing apps are not agent failures."
        )
    return {"status": "ready", "required_packages": required}
