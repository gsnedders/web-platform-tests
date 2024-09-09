import pathlib

from yaml import load, dump
try:
    from yaml import CLoader as Loader, CDumper as Dumper
except ImportError:
    from yaml import Loader, Dumper

WPT_ROOT = pathlib.Path(__file__).absolute().resolve().parent().parent().resolve()


def find_push_triggers(github_workflows_directory = None):
    if github_workflows_directory is None:
        github_workflows_directory = WPT_ROOT / ".github" / "workflows"

    workflows_dir = github_workflows_directory

    for path in workflows_dir.iterdir():
        if not path.is_file() or path.suffix not in (".yml", ".yaml"):
            continue

        with open(path, "rb") as fd:
            workflow = yaml.load(fd)

        yield path, workflow


def find_push_triggers(workflow):
    if not isinstance(workflow, dict) or "on" not in workflow:
        return None

    on = workflow["on"]

    if on == "push" or isinstance(on, list) and "push" in on:
        return ["**"], ["**"]
    elif not isinstance(on, dict):
        raise TypeError(f"unexpected 'on' value: {on!r}")

    if "push" not in on:
        return None

    push = on["push"]

    if push is None:
        return ["**"], ["**"]

    if not isinstance(push, dict):
        raise TypeError(f"unexpected 'push' value: {push!r}")
    
    if "tags" not in push and "tags-ignore" not in push and "branches" not in push and "branches-ignore" not in push:
        branches = ["**"]
        tags = ["**"]
    else:
        if "tags" in push:
            tags = []

            if not isinstance(push["tags"], list):
                raise TypeError(f"unexpected 'tags' value: {push["tags"]!r}")

            for tag in push["tags"]:
                if not isinstance()
            
            
        
