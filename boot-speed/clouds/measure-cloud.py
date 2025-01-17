#!/usr/bin/env python3
"""
Measure the boot speed of cloud instances.

Copyright 2019-2020 Canonical Ltd.
Paride Legovini <paride.legovini@canonical.com>
"""

import argparse
import datetime as dt
import glob
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

from pathlib import Path
from socket import timeout

import distro_info
import paramiko
import pycloudlib

from botocore.exceptions import ClientError

from paramiko.ssh_exception import (
    AuthenticationException,
    SSHException,
    NoValidConnectionsError,
)


known_clouds = ["kvm", "lxd", "ec2", "gce"]
distro_metanames = ("lts", "stable", "latest", "devel")
job_timestamp = dt.datetime.utcnow()


class EC2Instspec:
    cloud = "ec2"

    def __init__(
        self,
        *,
        name,
        release,
        inst_type,
        region,
        ec2_subnetid,
        ec2_sgid,
        ec2_availability_zone,
        ssh_pubkey_path,
        ssh_privkey_path,
        ssh_keypair_name
    ):
        # Defaults. They can't be set as keyword argument defaults because
        # we're always passing all the arguments to __init__, even if they
        # are None. And they can't be set as the argparse default values,
        # as different coulds need different defaults.
        self.name = name
        self.region = "us-east-1"
        self.inst_type = inst_type
        self.subnetid = ec2_subnetid
        self.sgid = ec2_sgid
        self.availability_zone = ec2_availability_zone
        self.ssh_pubkey_path = ssh_pubkey_path
        self.ssh_privkey_path = ssh_privkey_path
        self.ssh_keypair_name = ssh_keypair_name

        # User-specified settings
        self.release = release

        if region:
            self.region = region

        if name:
            self.name = name
        else:
            self.name = "-".join(
                ["bootspeed", self.cloud, self.inst_type.replace(".", ""), self.release]
            )

    def debian_sid_daily_image(self, arch):
        # https://noah.meyerhans.us/2020/03/04/daily-vm-image-builds-are-available-from-the-cloud-team/
        if arch == "amd64":
            arch = "x86_64"

        cmd = [
            "aws",
            "ec2",
            "describe-images",
            "--owner",
            "903794441882",
            "--region",
            self.region,
            "--output",
            "json",
            "--query",
            "Images[?Architecture=='"
            + arch
            + "']"
            + " | [?starts_with(Name, 'debian-sid-')] | max_by([], &Name)",
        ]

        daily_md = subprocess.check_output(cmd, universal_newlines=True)
        daily_md = json.loads(daily_md)
        ami = daily_md["ImageId"]
        serial = re.search(
            r"daily build ([0-9]+-[0-9]+)", daily_md["Description"]
        ).group(1)

        return ami, serial

    def measure(self, datadir, instances=1, reboots=1):
        """
        Measure Amazon AWS EC2.
        Returns the measurement metadata as a dictionary
        """
        print("Perforforming measurement on Amazon EC2")

        if self.release in distro_metanames:
            release = metaname2release(self.release)
            print("Resolved %s to %s" % (self.release, release))
        else:
            release = self.release

        ec2 = pycloudlib.EC2(tag=self.name, region=self.region)

        if not self.ssh_pubkey_path:
            self.ssh_pubkey_path = ec2.key_pair.public_key_path
        if not self.ssh_privkey_path:
            self.ssh_privkey_path = ec2.key_pair.private_key_path
        if not self.ssh_keypair_name:
            self.ssh_keypair_name = ec2.key_pair.name
        ec2.use_key(self.ssh_pubkey_path, self.ssh_privkey_path, self.ssh_keypair_name)

        # Will also catch unknown inst types raising a meaningful exception
        inst_specs = ec2.client.describe_instance_types(InstanceTypes=[self.inst_type])[
            "InstanceTypes"
        ][0]

        arch = inst_specs["ProcessorInfo"]["SupportedArchitectures"][0]
        if arch == "i386":
            arch = "x86_64"

        image_username = "ubuntu"

        if release.startswith("debian-"):
            image_username = "admin"
            debrelease = re.search(r"debian-(.*)", release).group(1)
            if debrelease == "sid":
                daily, serial = self.debian_sid_daily_image(arch)
            else:
                raise NotImplementedError
        else:
            daily = ec2.daily_image(release=release, arch=arch)
            serial = ec2.image_serial(daily)

        print("Instance architecture:", arch)
        print("Daily image for", release, "is", daily)
        print("Image serial:", serial)
        print("Instance username:", image_username)

        for ninstance in range(instances):
            instance_data = Path(datadir, "instance_" + str(ninstance))
            instance_data.mkdir()

            print("Launching instance", ninstance + 1, "of", instances, "tag:", ec2.tag)
            instance = ec2.launch(
                image_id=daily,
                instance_type=self.inst_type,
                SubnetId=self.subnetid,
                SecurityGroupIds=self.sgid,
                Placement={"AvailabilityZone": self.availability_zone},
                wait=False,
            )
            instance.username = image_username

            try:
                measure_instance(instance, instance_data, reboots)

                # If the availability zone is not specified a random one is
                # assigned. We want to make sure the next instances (if any)
                # will use the same zone, so we save it.
                if not self.availability_zone:
                    self.availability_zone = instance.availability_zone
            finally:
                print("Deleting the instance.")
                instance.delete(wait=False)

        metadata = gen_metadata(
            cloud=self.cloud,
            region=self.region,
            availability_zone=self.availability_zone,
            inst_type=self.inst_type,
            release=self.release,
            cloudid=daily,
            serial=serial,
        )

        return metadata


class LXDInstspec:
    cloud = "lxd"
    is_vm = False

    def __init__(self, *, name, release, inst_type, ssh_pubkey_path, ssh_privkey_path):
        self.name = name
        self.inst_type = inst_type
        self.release = release
        self.ssh_pubkey_path = ssh_pubkey_path
        self.ssh_privkey_path = ssh_privkey_path

        if name:
            self.name = name
        else:
            self.name = "-".join(
                ["bootspeed", self.cloud, self.inst_type.replace(".", ""), self.release]
            )

    def measure(self, datadir, instances=1, reboots=1):
        """
        Measure LXD containers.
        Returns the measurement metadata as a dictionary
        """
        print("Perforforming measurement on LXD")

        if self.release in distro_metanames:
            release = metaname2release(self.release)
            print("Resolved %s to %s" % (self.release, release))
        else:
            release = self.release

        if self.is_vm:
            lxd = pycloudlib.LXDVirtualMachine(tag=self.name, timestamp_suffix=False)
        else:
            lxd = pycloudlib.LXDContainer(tag=self.name, timestamp_suffix=False)

        lxd.key_pair = pycloudlib.key.KeyPair(
            self.ssh_pubkey_path, self.ssh_privkey_path
        )
        image = lxd.daily_image(release=release)
        serial = lxd.image_serial(image)

        print("Daily image for", release, "is", image)
        print("Image serial:", serial)

        for ninstance in range(instances):
            instance_data = Path(datadir, "instance_" + str(ninstance))
            instance_data.mkdir()

            print("Launching instance", ninstance + 1, "of", instances)
            instance = lxd.launch(
                image_id=image,
                instance_type=self.inst_type,
                name=self.name,
                ephemeral=True,
            )
            print("Instance launched (%s)" % self.name)

            try:
                measure_instance(instance, instance_data, reboots)
            finally:
                print("Deleting the instance.")
                instance.delete()

        # On LXD we can consider the machine the measurement is run on as the
        # 'region'; platform.node() returns its hostname.
        region = platform.node()
        metadata = gen_metadata(
            cloud=self.cloud,
            region=region,
            inst_type=self.inst_type,
            release=self.release,
            cloudid=image,
            serial=serial,
        )

        return metadata


class KVMInstspec(LXDInstspec):
    cloud = "kvm"
    is_vm = True


def ssh_hammer(instance):
    # Hammer the instance via SSH to record the first SSH login time.
    print("SSH-hammering instance")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    private_key = paramiko.RSAKey.from_private_key_file(
        instance.key_pair.private_key_path
    )

    timeout_start = time.time()
    # Let's be patient here: metal instances are slow to start.
    timeout_delta = 900

    while time.time() < timeout_start + timeout_delta:
        instip = instance.ip
        if not instip:
            time.sleep(0.5)
            continue

        try:
            client.connect(
                username=instance.username,
                hostname=instip,
                pkey=private_key,
                timeout=1,
                banner_timeout=1,
                auth_timeout=1,
            )
            client.exec_command("true")
        except (
            timeout,
            AuthenticationException,
            SSHException,
            EOFError,
            NoValidConnectionsError,
            ConnectionResetError,
            ClientError,
        ):
            pass
        else:
            client.close()
            return

    raise TimeoutError("timeout while hammering SSH")


def measure_instance(instance, datadir, reboots=1):
    print("*** Measuring instance ***")

    # Use the same command (and hence format) used when measuring devices
    os.system("date --utc --rfc-3339=ns > " + str(Path(datadir, "job-start-timestamp")))

    ssh_hammer(instance)

    instance.execute(
        "wget https://raw.githubusercontent.com/canonical/"
        "server-test-scripts/master/boot-speed/bootspeed.sh"
    )
    instance.execute("chmod +x bootspeed.sh")

    for nboot in range(0, reboots + 1):
        print("Measuring boot %d" % nboot)

        if nboot > 0:
            instance.restart(wait=True)
            ssh_hammer(instance)

        outstr = instance.execute("./bootspeed.sh 2>&1")
        print(outstr)
        outstr = instance.execute("find artifacts")
        print("----- remote listing")
        print(outstr)
        print("----- end of remote listing")

        # Test for the existence of the file bootspeed.sh creates if it
        # reached the end of the measurement with no errors.
        outstr = instance.execute(
            "test -f artifacts/measurement-successful" " && echo ok"
        )
        if outstr == "ok":
            print("artifacts/measurement-successful present => SUCCESS")
        else:
            print("Measurement failed (missing measurement-successful)!")
            sys.exit(1)

        bootdir = "boot_" + str(nboot)
        print("Prepare the measurement data tarball")
        instance.execute("mv artifacts " + bootdir)
        instance.execute("tar czf " + bootdir + ".tar.gz " + bootdir)
        print("Pull the tarball")
        instance.pull_file(bootdir + ".tar.gz", bootdir + ".tar.gz")

    for tarball in glob.glob("boot_*.tar.gz"):
        with tarfile.open(tarball, "r:gz") as tar:
            def is_within_directory(directory, target):
                
                abs_directory = os.path.abspath(directory)
                abs_target = os.path.abspath(target)
            
                prefix = os.path.commonprefix([abs_directory, abs_target])
                
                return prefix == abs_directory
            
            def safe_extract(tar, path=".", members=None, *, numeric_owner=False):
            
                for member in tar.getmembers():
                    member_path = os.path.join(path, member.name)
                    if not is_within_directory(path, member_path):
                        raise Exception("Attempted Path Traversal in Tar File")
            
                tar.extractall(path, members, numeric_owner=numeric_owner) 
                
            
            safe_extract(tar, path=datadir)
        os.unlink(tarball)


def gen_metadata(
    *, cloud, region, availability_zone="", inst_type, release, cloudid, serial
):
    """Returns the instance metadata as a dictionary"""
    date = job_timestamp.strftime("%Y%m%d%H%M%S")
    isodate = job_timestamp.isoformat()

    metadata = {}
    metadata["date"] = date
    metadata["date-rfc3339"] = isodate
    metadata["type"] = "cloud"
    metadata["instance"] = {
        "cloud": cloud,
        "region": region,
        "availability_zone": availability_zone,
        "instance_type": inst_type,
        "release": release,
        "cloudimage_id": cloudid,
        "image_serial": serial,
    }

    return metadata


def gen_archivename(metadata):
    """Generate a standardized measurement directory (and tarball) name"""
    date = metadata["date"]
    cloud = metadata["instance"]["cloud"]
    inst_type = metadata["instance"]["instance_type"]
    release = metadata["instance"]["release"]

    arcname = cloud + "-" + inst_type + "-" + release + "_" + date
    return arcname


def metaname2release(metaname):
    if metaname == "latest":
        # 'all' is a list of codenames of all known releases, including
        # the development one, in the release order.
        return distro_info.UbuntuDistroInfo().all[-1]

    return getattr(distro_info.UbuntuDistroInfo(), metaname)()


def main():
    args = parse_args()

    if args.cloud not in known_clouds:
        print("Unknown cloud provider:", args.cloud)
        sys.exit(1)

    if args.cloud == "ec2":
        instspec = EC2Instspec(
            name=args.name,
            release=args.release,
            inst_type=args.inst_type,
            region=args.region,
            ec2_subnetid=args.ec2_subnetid,
            ec2_sgid=args.ec2_sgid,
            ec2_availability_zone=args.ec2_availability_zone,
            ssh_pubkey_path=args.ssh_pubkey_path,
            ssh_privkey_path=args.ssh_privkey_path,
            ssh_keypair_name=args.ssh_keypair_name,
        )
    elif args.cloud == "lxd":
        instspec = LXDInstspec(
            name=args.name,
            release=args.release,
            inst_type=args.inst_type,
            ssh_pubkey_path=args.ssh_pubkey_path,
            ssh_privkey_path=args.ssh_privkey_path,
        )
    elif args.cloud == "kvm":
        instspec = KVMInstspec(
            name=args.name,
            release=args.release,
            inst_type=args.inst_type,
            ssh_pubkey_path=args.ssh_pubkey_path,
            ssh_privkey_path=args.ssh_privkey_path,
        )
    else:
        raise NotImplementedError

    tmp_datadir = tempfile.mkdtemp(prefix="bootspeed-", dir=os.getcwd())

    logging.basicConfig(level=logging.INFO)
    metadata = instspec.measure(tmp_datadir, args.instances, args.reboots)

    with open(Path(tmp_datadir, "metadata.json"), "w") as mdfile:
        json.dump(metadata, mdfile)

    archivename = gen_archivename(metadata)
    with tarfile.open((archivename + ".tar.gz"), "w:gz") as tar:
        tar.add(tmp_datadir, arcname=archivename)

    shutil.rmtree(tmp_datadir)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", "--name", help="Instance name", default=None)
    parser.add_argument(
        "-c", "--cloud", help="Cloud to measure", choices=known_clouds, required=True
    )
    parser.add_argument("-t", "--inst-type", help="Instance type", default="t2.micro")
    parser.add_argument(
        "-r", "--release", help="Ubuntu release to measure", required=True
    )
    parser.add_argument("--reboots", help="Number of reboots", default=1, type=int)
    parser.add_argument("--instances", help="Number of instances", default=1, type=int)
    parser.add_argument(
        "--ssh-pubkey-path",
        help="Override pycloudlib's " "default for the SSH public key to use",
        default=None,
    )
    parser.add_argument(
        "--ssh-privkey-path",
        help="Override pycloudlib's " "default for the SSH private key to sue",
        default=None,
    )
    parser.add_argument(
        "--ssh-keypair-name",
        help="Override pycloudlib's " " default for the SSH keypair name",
        default=None,
    )
    parser.add_argument("--ec2-subnetid", help="AWS EC2 SubnetId", default="")
    parser.add_argument(
        "--ec2-availability-zone", help="AWS EC2 Availability Zone", default=""
    )
    parser.add_argument(
        "--ec2-sgid", help="AWS EC2 SecurityGroupId", action="append", default=[]
    )
    parser.add_argument("--region", help="Cloud region")
    args = parser.parse_args()
    return args


if __name__ == "__main__":
    main()
