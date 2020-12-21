# arch-netboot-builder

**WIP: This readme is not yet complete.**

CAUTION: This script expects to run in a fairly complex environment, and will likely not do what you want out of the box. This readme attempts to document the setup process, as well as changes you might want to make, but it will never be complete- this repository is best used as an example from which you can build your own scripts. The required configuration is also likely less secure than a stock configuration, so be careful if either machine is publicly accessible.

## Environment
### Build machine
The build machine should be running a fairly up-to-date version of Arch Linux.

The contents of this repository should reside on the root of a ZFS filesystem. The path to this filesystem should be stored in the `ZFS_CWD` variable in `build_config.py`

The local root user should have an SSH key.

### NAS machine
The NAS machine should not have strict operating system requirements. This setup has been tested on a 2020 version of Arch, as well as Ubuntu Server 18.04.

Python3 must be installed.

No existing TFTP server may be installed.

A ZFS filesystem should be created, and `tftpserv.py` should be placed in the root of this filesystem and set to run at boot. This filesystem should be stored in the `NAS_IMAGE_PATH` and `ZFS_NAS_IMAGE_PATH` variables in `build_config.py`

A user account on the NAS machine should have ownership of the `NAS_IMAGE_PATH` directory.
This user's name should be stored in the `NAS_USER` variable in `build_config.py`.

The NAS's IP address should be stored in the `NAS_IP` variable in `build_config.py`.

The `NAS_USER` account on the NAS machine should be set up to recognize the build machine's root SSH key, and sudo should be configured to not require a password from this user.

## Invocation
`./build_image.py` starts the build.

Pressing ^C will stop the build at the end of the current stage. Pressing it again will stop it immediately.
CAUTION: Pressing ^C twice does not always cleanly stop the build. It may be necessary to manually unmount any filesystems left mounted in the build target.

`./clean_image.py` empties the build directory to prepare for a fresh build.
