#!/usr/bin/env python3

"""
This file is part of my dotfiles (https://github.com/jakobbbb/dotfiles).
Copyright (C) 2017 - 2026 Jakob Ruckel

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

import os
import textwrap
from enum import Enum
from collections import Counter

DeployStatus = Enum(
    "DeployStatus", ["AlreadyGood", "MovedOriginal", "CreatedNew", "Failure"]
)


# Any files or directories whose path starts with any of these will be deployed.
# Directories are deployed recursively.
DEPLOY_THIS = (
    ".config",
    # TODO Add anyhting you need right here :3
)

HOMEDIR = os.path.expanduser("~") + "/"
DOTFILEDIR = os.path.dirname(os.path.realpath(__file__))


def deploy_file(source, target):
    """
    Deploy a single file by symlinking.
    """
    status = DeployStatus.CreatedNew
    target_dir = os.path.dirname(target)

    if os.path.lexists(target):
        if os.path.realpath(target) == os.path.realpath(source):
            status = DeployStatus.AlreadyGood
            return status
        else:
            target_bak = target + ".bak"
            print(
                f"Warning: File '{target}' exists, "
                "but is not the correct symlink. "
            )
            print(f"    should be: {source}")
            print(f"    but it is: {os.path.realpath(target)}")
            os.rename(target, target_bak)
            status = DeployStatus.MovedOriginal
    elif not os.path.exists(target_dir):
        os.makedirs(target_dir)

    os.symlink(source, target)
    return status


def print_deploy_error(source, target, error):
    print("Error while deploying:")
    print(f"    source: {source}")
    print(f"    target: {target}")
    print(textwrap.indent(str(error), "    "))
    print()


def main():
    stats = []
    os.chdir(DOTFILEDIR)
    for root, _, files in os.walk(".", topdown=True):
        for name in files:
            file_name = os.path.join(root, name)  # starts with ./
            file_short = file_name[2:]  # ./ removed
            root_short = root[2:]  # ./ removed
            target = None
            source = os.path.join(DOTFILEDIR, file_short)
            target = os.path.join(HOMEDIR, file_short)

            if not file_short.startswith(DEPLOY_THIS):
                continue

            # TODO you can delete this if you don't have a userChrome.css
            if file_short.startswith(".mozilla"):
                firefox_profile = get_firefox_profile()
                if firefox_profile:
                    target = target.replace("profile", firefox_profile)
                    root_short = root_short.replace("profile", firefox_profile)
                else:
                    stats.append(DeployStatus.Failure)
                    print_deploy_error(
                        source, target, "Could not find Firefox profile"
                    )
                    continue

            try:
                result = deploy_file(source, target)
                stats.append(result)
            except Exception as e:
                print_deploy_error(source, target, e)

    for status, n in Counter(stats).most_common():
        o = len(DeployStatus.__name__) + 1
        print(f"{str(status)[o:]}: {n} files")
    print("Deployed dotfiles :3")


if __name__ == "__main__":
    main()

