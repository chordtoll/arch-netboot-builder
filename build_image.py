#!/usr/bin/env python
import os, sys
from pathlib import Path
import filecmp
import shutil
import glob
import subprocess
import time
import shlex
import io
import selectors
import signal

from build_config import *

echo=False
cur_stage="NONE"

def take_snapshot():
    if cur_stage in ['makerootfs'] or cur_stage.startswith('packages-') or cur_stage.startswith('file: '): return
    print(f"    Snapshot: {cur_stage}")
    assert(os.system(f'zfs snapshot {ZFS_CWD}/.install@{cur_stage}')==0)

def rollback_snapshot():
    if cur_stage in ['makerootfs'] or cur_stage.startswith('packages-') or cur_stage.startswith('file: '): return
    print(f"    Rollback: {cur_stage}")
    assert(os.system(f'zfs rollback {ZFS_CWD}/.install@{cur_stage}')==0)
    assert(os.system(f'zfs destroy {ZFS_CWD}/.install@{cur_stage}')==0)

def commit_snapshot():
    cur_time = time.time()
    if cur_stage in ['makerootfs'] or cur_stage.startswith('packages-') or cur_stage.startswith('file: '): return
    print(f"    Commit: {cur_stage}")
    assert(os.system(f'zfs destroy {ZFS_CWD}/.install@{cur_stage}')==0)

class GracefulInterruptHandler(object):

    def __init__(self, sig=signal.SIGINT):
        self.sig = sig

    def __enter__(self):

        self.interrupted = False
        self.released = False

        self.original_handler = signal.getsignal(self.sig)

        def handler(signum, frame):
            print("    Terminating at end of stage.")
            self.release()
            self.interrupted = True

        signal.signal(self.sig, handler)

        return self

    def __exit__(self, type, value, tb):
        self.release()

    def release(self):

        if self.released:
            return False

        signal.signal(self.sig, self.original_handler)

        self.released = True

        return True

# This script must be run as root!
if not os.geteuid()==0:
    sys.exit('This script must be run as root!')

cwd = Path(os.getcwd())
root = cwd / '.install'

class buildstage():
    def stagename(self):
        assert(False)
    def deps(self):
        return []
    def execute(self,handler=None):
        assert(False)
    def test(self):
        return os.path.isfile(root/'.install'/self.stagename())

    def capture_subprocess_output(self,subprocess_args,shell,print_out=True,print_err=True):
        
        if echo: print(subprocess_args)
        # Start subprocess
        # bufsize = 1 means output is line buffered
        # universal_newlines = True is required for line buffering
        process = subprocess.Popen(subprocess_args,
                                   shell=shell,
                                   bufsize=1,
                                   preexec_fn=os.setpgrp,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE,
                                   universal_newlines=True,
                                   errors='ignore')

        # Create callback function for process output
        buf = io.StringIO()
        def handle_stdout(stream, mask):
            nonlocal print_out
            # Because the process' output is line buffered, there's only ever one
            # line to read when this function is called
            line = stream.readline()
            if line:
                buf.write("O: "+line)
            if line and print_out:
                sys.stdout.write(f'    {cur_stage:20s} | {line}')

        def handle_stderr(stream, mask):
            nonlocal print_err
            # Because the process' output is line buffered, there's only ever one
            # line to read when this function is called
            line = stream.readline()
            if line:
                buf.write("E: "+line)
            if line and print_err:
                sys.stderr.write(f'    {cur_stage:20s} > {line}')

        # Register callback for an "available for read" event from subprocess' stdout stream
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, handle_stdout)
        selector.register(process.stderr, selectors.EVENT_READ, handle_stderr)

        # Loop until subprocess is terminated
        while process.poll() is None:
            # Wait for events and handle them with their registered callbacks
            events = selector.select()
            for key, mask in events:
                callback = key.data
                callback(key.fileobj, mask)

        # Get process return code
        return_code = process.wait()
        selector.close()

        # Store buffered output
        output = buf.getvalue()
        buf.close()

        return (return_code, output)

    def run_cmd(self,cmd,test=False,quiet=False,silent=False,log=None):
        #print(cmd)
        so=not (quiet or silent)
        se=not silent
        #print(cmd)
        rc,output = self.capture_subprocess_output(cmd, shell=True, print_out = so, print_err = se)
        if log:
            with open(log,'w') as f:
                f.write(output)
        if not test: assert(rc==0)
        return rc
    def run_chroot(self,cmd,test=False,quiet=False,silent=False,log=None):
        return self.run_cmd('arch-chroot "%s" bash -c %s'%(root,shlex.quote(cmd)),test,quiet,silent,log)
    def run_remote(self,cmd,test=False,quiet=False,silent=False,log=None):
        cmd_quoted=shlex.quote(cmd)
        return self.run_cmd(f'sudo -u {NAS_USER} ssh -n -o ForwardX11=no -t {NAS_IP} {cmd_quoted}',test,quiet,silent,log)
    def capture_cmd(self,cmd,test=False,silent=False):
        #print(cmd)
        #assert(False)
        result = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL if silent else None)
        if test:
            return result.stdout.decode(),result.returncode
        else:
            assert(result.returncode==0)
            return result.stdout.decode()
    def capture_chroot(self,cmd,test=False,silent=False):
        return self.capture_cmd('arch-chroot "%s" %s'%(root,cmd),test)
    def capture_remote(self,cmd,test=False,silent=False):
        cmd_quoted=shlex.quote(cmd)
        return self.capture_cmd(f'sudo -u {NAS_USER} ssh -o ForwardX11=no -t {NAS_IP} {cmd_quoted}',test,silent)
    def test_equal(self,a,b):
        with open(a,'rb') as f:
            ca=f.read()
        try:
            with open(b,'rb') as f:
                cb=f.read()
        except:
            return False
        return ca==cb
    def mark_complete(self):
        with open(root/'.install'/self.stagename(),'w'):
            pass

class stageInstallFile(buildstage):
    def stagename(self):
        return "file: "+self.toInstall()[1]
    def test(self):
        f,t = self.toInstall()
        t=t.lstrip('/')
        return self.test_equal(f,root/t)
    def execute(self,handler):
        f,t = self.toInstall()
        t=t.lstrip('/')
        shutil.copy(f,root/t)

class stageRootFS(buildstage):
    def stagename(self):
        return 'makerootfs'
    def execute(self,handler):
        self.run_cmd(f"zfs create {ZFS_CWD}/.install")
        self.run_cmd("mkdir %s"%(root/".install"))
        self.mark_complete()

class stagePacstrap(buildstage):
    def stagename(self):
        return 'pacstrap'
    def deps(self):
        return [stageRootFS]
    def execute(self,handler):
        self.run_cmd(f"/usr/bin/time -f '%U %S %e %E %P %X %D %M %I %O %F %R %W' -o {root}/.install/pacstrap.time pacstrap -C ./pacman.conf -d {root} base linux linux-headers linux-firmware mkinitcpio-nfs-utils nfs-utils wget zsh grub sudo sshfs base-devel git time", log=root/".install/pacstrap.log")
        self.mark_complete()

class stageSublimeKey(buildstage):
    def stagename(self):
        return 'sublime-key'
    def deps(self):
        return [stagePacstrap]
    def execute(self,handler):
        self.run_chroot('wget https://download.sublimetext.com/sublimehq-pub.gpg')
        self.run_chroot('pacman-key --add sublimehq-pub.gpg')
        self.run_chroot('pacman-key --lsign-key 8A8F901A')
        self.run_chroot('rm sublimehq-pub.gpg')
        self.mark_complete()

class stagePacmanConf(stageInstallFile):
    def deps(self):
        return [stagePacstrap]
    def toInstall(self):
        return 'pacman.conf','/etc/pacman.conf'

class stageInitramfs(buildstage):
    def stagename(self):
        return 'update 1'
    def deps(self):
        return [stagePacstrap]
    def execute(self,handler):
        self.run_chroot('sed s/nfsmount/mount.nfs4/ "/usr/lib/initcpio/hooks/net" > "/usr/lib/initcpio/hooks/netnfs4"')
        self.run_chroot('cp /usr/lib/initcpio/install/net{,nfs4}')
        file='packages/overlayroot-0.2-2-any.pkg.tar.zst'
        self.run_cmd(f'/usr/bin/time -f \'%U %S %e %E %P %X %D %M %I %O %F %R %W\' -o {root}/.install/initramfs.overlayroot.time pacman --noconfirm --needed --root "{root}" --dbpath "{root}/var/lib/pacman" -U {file}', log=root/".install/initramfs.overlayroot.log")
        self.mark_complete()

class stageMkinitcpioConf(stageInstallFile):
    def deps(self):
        return [stageInitramfs]
    def toInstall(self):
        return 'mkinitcpio.conf','/etc/mkinitcpio.conf'

class stageUpdate1(buildstage):
    def stagename(self):
        return 'update1'
    def deps(self):
        return [stagePacstrap,stageSublimeKey,stagePacmanConf]
    def execute(self,handler):
        self.run_chroot("pacman -Syu")
        self.mark_complete()

class stageGrub(buildstage):
    def stagename(self):
        return 'grub'
    def deps(self):
        return [stagePacstrap]
    def execute(self,handler):
        self.run_chroot('grub-mknetdir --net-directory=/boot --subdir=grub')
        self.mark_complete

class stageFstab(stageInstallFile):
    def deps(self):
        return [stagePacstrap]
    def toInstall(self):
        return 'fstab','/etc/fstab'

class stageSystemSetup(buildstage):
    def stagename(self):
        return 'system-setup'
    def deps(self):
        return [stageFstab]
    def execute(self,handler):
        self.run_chroot('echo "nameserver 8.8.8.8" >> /etc/resolv.conf')
        self.run_chroot('ln -sfv /usr/share/zoneinfo/US/Pacific /etc/localtime')
        self.run_chroot('hwclock --systohc')
        self.run_chroot('echo "en_US.UTF-8 UTF-8">  /etc/locale.gen')
        self.run_chroot('locale-gen')
        self.run_chroot('echo "LANG=en_US.UTF-8" >  /etc/locale.conf')
        self.run_chroot('echo "icarus-nfs" > /etc/hostname')
        self.run_chroot('echo "127.0.0.1    localhost" >   /etc/hosts')
        self.run_chroot('echo "::1    localhost" >>  /etc/hosts')
        self.run_chroot('echo "127.0.1.1    icarus-nfs" >> /etc/hosts')
        self.run_chroot('echo "blacklist pcspkr" | tee /etc/modprobe.d/nobeep.conf')

class stageSudoers(stageInstallFile):
    def deps(self):
        return [stagePacstrap]
    def toInstall(self):
        return 'sudoers-nopass','/etc/sudoers'

class stageUser(buildstage):
    def stagename(self):
        return 'user'
    def deps(self):
        return [stageSudoers,stageSystemSetup]
    def execute(self,handler):
        self.run_chroot('groupadd sudo')
        self.run_chroot(f'useradd -m -G sudo {INNER_USER}')
        with open(root/'password.file','w') as f:
            f.write(f'root:{ROOT_PASSWORD}\n')
            f.write(f'{INNER_USER}:{INNER_PASSWORD}\n')
        self.run_chroot('cat /password.file | chpasswd')
        self.run_chroot('rm /password.file')
        self.run_chroot(f'chsh -s /bin/zsh {INNER_USER}')
        self.mark_complete()

class stageMakepkgConf(stageInstallFile):
    def deps(self):
        return [stageUser]
    def toInstall(self):
        return 'makepkg1.conf','/etc/makepkg.conf'

class stageTrizen(buildstage):
    def stagename(self):
        return 'trizen'
    def deps(self):
        return [stageMakepkgConf]
    def execute(self,handler):
        self.run_chroot(f'sudo -u {INNER_USER} mkdir -p /home/{INNER_USER}/build')
        self.run_chroot(f'cd /home/{INNER_USER}/build; sudo -u {INNER_USER} git clone https://aur.archlinux.org/trizen.git')
        self.run_chroot(f'/usr/bin/time -f \'%U %S %e %E %P %X %D %M %I %O %F %R %W\' -o /.install/trizen.time bash -c "cd /home/{INNER_USER}/build/trizen; sudo -u {INNER_USER} makepkg --noconfirm -si"', log=root/".install/trizen.log")
        self.run_chroot(f'mkdir -p /home/{INNER_USER}/.config/trizen/')
        self.mark_complete()

class stageTrizenConf(stageInstallFile):
    def deps(self):
        return [stageTrizen]
    def toInstall(self):
        return 'trizen.conf',f'/home/{INNER_USER}/.config/trizen/trizen.conf'

class stageMountpoints(buildstage):
    def stagename(self):
        return 'mountpoints'
    def deps(self):
        return [stagePacstrap]
    def test(self):
        return os.path.isdir(root/'tank')
    def execute(self,handler):
        self.run_chroot('mkdir /tank')
        self.run_chroot('mkdir /depot')
        self.run_chroot('mkdir /athena')
        self.run_chroot(f'chown {INNER_USER}:{INNER_USER} /tank')
        self.run_chroot(f'chown {INNER_USER}:{INNER_USER} /depot')
        self.run_chroot(f'chown {INNER_USER}:{INNER_USER} /athena')

class stagePackageKeys(buildstage):
    def stagename(self):
        return 'packages-keys'
    def deps(self):
        return [stageTrizenConf]
    def execute(self,handler):
        with open('keys.txt','r') as f:
            for line in f:
                line=line.strip()
                if not line: continue
                if self.run_chroot(f"sudo -u {INNER_USER} gpg --export -a {line} | grep 'BEGIN PGP PUBLIC KEY BLOCK'",test=True,quiet=True)!=0:
                    print("\tInstalling key",line)
                    cur_stage=f'PK-{line}'
                    take_snapshot()
                    try:
                        self.run_chroot(f"sudo -u {INNER_USER} gpg --recv-keys --keyserver keys.gnupg.net {line}")
                    except:
                        rollback_snapshot()
                        raise
                    else:
                        commit_snapshot()


class stagePackagesEarly(buildstage):
    def stagename(self):
        return 'packages-early'
    def deps(self):
        return [stagePackageKeys]
    def execute(self,handler):
        self.run_chroot('mkdir -p /.install/packages/complete/early')
        self.run_chroot('mkdir -p /.install/packages/logs/early')
        self.run_chroot('mkdir -p /.install/packages/times/early')
        for i in sorted(glob.glob('packages/E*')):
            _,fn = os.path.split(i)
            cur_stage=f'PE-{i}'
            if os.path.isfile(root/".install/packages/complete/early"/fn):
                continue
            self.run_cmd(f'/usr/bin/time -f \'%U %S %e %E %P %X %D %M %I %O %F %R %W\' -o "{root}/.install/packages/times/early/{fn}" pacman --noconfirm --needed --root "{root}" --dbpath "{root}/var/lib/pacman" -U {i}', log=root/".install/packages/logs/early"/fn)
            with open(root/".install/packages/complete/early"/fn,'w'):
                pass

class stagePackagesMain(buildstage):
    def stagename(self):
        return 'packages-main'
    def deps(self):
        return [stagePackagesEarly]
    def execute(self,handler):
        global cur_stage
        #assert(False)
        self.run_chroot('mkdir -p /.install/packages/complete/main')
        self.run_chroot('mkdir -p /.install/packages/logs/main')
        self.run_chroot('mkdir -p /.install/packages/times/main')
        self.run_chroot('mkdir -p /.install/packagegroups/complete/main')
        self.run_chroot('mkdir -p /.install/packagegroups/logs/main')
        self.run_chroot('mkdir -p /.install/packagegroups/times/main')
        #assert(False)
        with open('packages.txt','r') as f:
            for line in f:
                if handler.interrupted:
                    return
                line = line.strip()
                if not line: continue
                if line[0]=='#': continue
                cur_stage=f'PM-{line}'
                if line.startswith('g:'):
                    line = line[2:]
                    if os.path.isfile(root/".install/packagegroups/complete/main"/line):
                        continue
                    print("\tTrying group",line)
                    packages=[]
                    for pkg in self.capture_chroot("pacman -Sg %s"%line).split('\n'):
                        if pkg.strip():
                            packages.append(pkg.split(' ')[1].strip())
                    if self.run_chroot("pacman -Qi %s"%' '.join(packages),test=True,silent=True)!=0:
                        pl=' '.join(packages)
                        take_snapshot()
                        try:
                            self.run_chroot(f"/usr/bin/time -f '%U %S %e %E %P %X %D %M %I %O %F %R %W' -o /.install/packagegroups/times/main/{line} sudo -u {INNER_USER} trizen --noconfirm --needed -S {pl}", log=root/".install/packagegroups/logs/main"/line)
                            with open(root/".install/packagegroups/complete/main"/line,'w'):
                                pass
                        except:
                            rollback_snapshot()
                            raise
                        else:
                            commit_snapshot()
                    else:
                        with open(root/".install/packagegroups/complete/main"/line,'w'):
                            pass
                else:
                    if os.path.isfile(root/".install/packages/complete/main"/line):
                        continue
                    print("\tTrying package",line)
                    #assert(self.run_chroot("pacman -Sg %s"%line,test=True)!=0)
                    if self.run_chroot("pacman -Qi %s"%line,test=True,silent=True)!=0:
                        take_snapshot()
                        try:
                            self.run_chroot(f"/usr/bin/time -f '%U %S %e %E %P %X %D %M %I %O %F %R %W' -o /.install/packages/times/main/{line} sudo -u {INNER_USER} trizen --noconfirm --needed -S {line}", log=root/".install/packages/logs/main"/line)
                            with open(root/".install/packages/complete/main"/line,'w'):
                                pass
                        except:
                            rollback_snapshot()
                            raise
                        else:
                            commit_snapshot()
                    else:
                        with open(root/".install/packages/complete/main"/line,'w'):
                            pass
        cur_stage = self.stagename()

class stagePackagesLate(buildstage):
    def stagename(self):
        return 'packages-late'
    def deps(self):
        return [stagePackagesMain]
    def execute(self,handler):
        self.run_chroot('mkdir -p /.install/packages/complete/late')
        self.run_chroot('mkdir -p /.install/packages/logs/late')
        self.run_chroot('mkdir -p /.install/packages/times/late')
        for i in sorted(glob.glob('packages/L*')):
            _,fn = os.path.split(i)
            cur_stage=f'PL-{i}'
            if os.path.isfile(root/".install/packages/complete/late"/fn):
                continue
            self.run_cmd(f'/usr/bin/time -f \'%U %S %e %E %P %X %D %M %I %O %F %R %W\' -o "{root}/.install/packages/times/late/{fn}" pacman --noconfirm --needed --root "{root}" --dbpath "{root}/var/lib/pacman" -U {i}', log=root/".install/packages/logs/late"/fn)
            with open(root/".install/packages/complete/late"/fn,'w'):
                pass

class stagePackages(buildstage):
    def stagename(self):
        return 'packages'
    def deps(self):
        return [stageUpdate1,stageTrizenConf,stageMountpoints,stagePackagesLate]
    def execute(self,handler):
        pass

class stageModFuse(buildstage):
    def stagename(self):
        return 'mod-fuse'
    def deps(self):
        return [stagePacstrap]
    def test(self):
        return os.path.isfile(root/'etc/modules-load.d/fuse.conf')
    def execute(self,handler):
        self.run_chroot('echo fuse > /etc/modules-load.d/fuse.conf')

class stageModZFS(buildstage):
    def stagename(self):
        return 'mod-zfs'
    def deps(self):
        return [stagePacstrap]
    def test(self):
        return os.path.isfile(root/'etc/modules-load.d/zfs.conf')
    def execute(self,handler):
        self.run_chroot('echo zfs > /etc/modules-load.d/zfs.conf')

class stageZpoolCache(stageInstallFile):
    def deps(self):
        return [stagePackages]
    def toInstall(self):
        return 'zpool.cache','/etc/zfs/zpool.cache'

class stageServices(buildstage):
    def stagename(self):
        return 'services'
    def deps(self):
        return [stagePackages]
    def execute(self,handler):
        self.run_chroot('systemctl enable sshd')
        self.run_chroot('systemctl enable docker')
        self.run_chroot('systemctl enable bumblebeed')
        self.mark_complete()

class stageGroups(buildstage):
    def stagename(self):
        return 'groups'
    def deps(self):
        return [stagePackages]
    def execute(self,handler):
        self.run_chroot(f'usermod -a -G uucp {INNER_USER}')
        self.run_chroot(f'usermod -a -G docker {INNER_USER}')
        self.run_chroot(f'usermod -a -G bumblebee {INNER_USER}')
        self.run_chroot(f'usermod -a -G plugdev {INNER_USER}')
        self.run_chroot(f'usermod -a -G realtime {INNER_USER}')
        self.run_chroot(f'usermod -a -G audio {INNER_USER}')
        self.mark_complete()

class stageInitCpio(buildstage):
    def stagename(self):
        return 'initcpio'
    def deps(self):
        return [stagePackages]
    def execute(self,handler):
        self.run_chroot('mkinitcpio -p linux')
        self.mark_complete()


class stageExtraRootFiles(buildstage):
    def stagename(self):
        return 'extra-rootfs-files'
    def deps(self):
        return [stagePackages]
    def execute(self,handler):
        self.run_cmd('rsync -av root_files/ %s/'%(root))

class stageCleanup(buildstage):
    def stagename(self):
        return 'cleanup'
    def deps(self):
        return [stagePackages,stageModFuse,stageModZFS,stageServices,stageZpoolCache,stageGroups,stageInitCpio,stageExtraRootFiles]
    def execute(self,handler):
        self.run_chroot('rm -v /etc/machine-id',test=True,silent=True)
        self.run_chroot(f'rm -rf /home/{INNER_USER}/.cache')
        self.run_chroot(f'rm -rf /home/{INNER_USER}/.trizensources')
        self.run_chroot('rm -rf /var/cache/pacman/pkg/*')

class stageFinish(buildstage):
    def stagename(self):
        return 'finish'
    def deps(self):
        return [stageCleanup]
    def execute(self,handler):
        global echo
        timestamp=int(time.time())
        print('Timestamp:',timestamp)
        parentimage = max(self.capture_remote(f'ls {NAS_IMAGE_PATH}/builds',silent=True).strip().split())
        print('Parent:',parentimage)
        #echo=True
        self.run_chroot(f'echo {timestamp} > /.install/version')
        self.run_remote(f'sudo zfs snapshot {ZFS_NAS_IMAGE_PATH}/builds/{parentimage}@{timestamp}')
        self.run_remote(f'sudo zfs clone {ZFS_NAS_IMAGE_PATH}/builds/{parentimage}@{timestamp} {ZFS_NAS_IMAGE_PATH}/builds/{timestamp}')
        self.run_remote(f'sudo zfs promote {ZFS_NAS_IMAGE_PATH}/builds/{timestamp}')
        self.run_cmd(f'rsync -ahxXSAHv --delete --rsync-path="sudo rsync" {root}/ {NAS_USER}@{NAS_IP}:{NAS_IMAGE_PATH}/builds/{timestamp}/ 2>&1 | tee rsync.log'
        self.run_cmd(f'rsync -v --rsync-path="sudo rsync" {cwd}/rsync.log {NAS_USER}@{NAS_IP}:{NAS_IMAGE_PATH}/builds/{timestamp}/.install/rsync.log')
        self.run_remote(f'echo {timestamp} > {NAS_IMAGE_PATH}/mounts/latest')
        self.run_remote(f'echo {timestamp} > {NAS_IMAGE_PATH}/mounts/c85b761a2c47')
        #echo=False

def run_build(stage):
    global cur_stage
    s=stage()
    print("Running stage",s.stagename())
    if s.test():
        print("\tAlready complete!")
        return
    for dep in s.deps():
        run_build(dep)
    print("Building stage",s.stagename())
    interrupted=False
    try:
        cur_stage = s.stagename()
        take_snapshot()
        with GracefulInterruptHandler() as h:
            s.execute(handler=h)
            if h.interrupted:
                interrupted=True
    except:
        rollback_snapshot()
        raise
    else:
        commit_snapshot()
    if interrupted:
        assert(False)
    print("\tDone!")

if __name__=="__main__":
    run_build(stageFinish)
