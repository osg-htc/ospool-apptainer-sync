#!/usr/bin/env python3

import os
import getpass
import logging
import socket
import re
import sys
import yaml
from pathlib import Path
from datetime import datetime, timezone

import htcondor
from Pegasus.api import *


class OSGSIFSync:
    # Class-level constants
    BASE_PATH = Path(__file__).parent.resolve()
    WORK_DIR = Path.home() / "workflows"
    SCRATCH_DIR = f"/ospool/ap20/data/{getpass.getuser()}/local-scratch"
    OUTPUT_DIR = f"/ospool/ap20/data/{getpass.getuser()}/apptainer-sync"
    REPO_DIR = "/ospool/uc-shared/public/OSG-Staff/images/v2"

    def __init__(self):
        """Initialize the workflow components."""
        self.runs_dir = self.WORK_DIR / "runs"
        self.scratch_dir = self.SCRATCH_DIR
        self.output_dir = self.OUTPUT_DIR
        self.recipe = None

        self.props = Properties()
        self.wf = Workflow("ospool-apptainer-sync")
        self.tc = TransformationCatalog()
        self.sc = SiteCatalog()
        self.rc = ReplicaCatalog()

        # Add catalogs to the workflow
        self.wf.add_transformation_catalog(self.tc)
        self.wf.add_site_catalog(self.sc)
        self.wf.add_replica_catalog(self.rc)

    def generate_props(self):
        """Generate Pegasus properties."""
        self.props["dagman.pull.maxjobs"] = "100"
        self.props["dagman.local-build.maxjobs"] = "1"
        self.props["pegasus.catalog.workflow.amqp.url"] = "amqp://friend:donatedata@msgs.pegasus.isi.edu:5672/prod/workflows"
        self.props["pegasus.data.configuration"] = "nonsharedfs"
        self.props["pegasus.dir.useTimestamp"] = "true"
        self.props.write()

    def generate_tc(self):
        """Generate the Transformation Catalog."""
        
        apptainer_build = Transformation(
            "apptainer-build",
            site="local",
            pfn=self.BASE_PATH / "bin/apptainer-build",
            is_stageable=True,
        ).add_profiles(Namespace.DAGMAN, key="category", value="local-build")

        docker_pull = Transformation(
            "docker-pull",
            site="local",
            pfn=self.BASE_PATH / "bin/docker-pull",
            is_stageable=True,
        ).add_profiles(Namespace.DAGMAN, key="category", value="pull") \
         .add_profiles(Namespace.CONDOR, key="+container_bypass", value="True") \
         .add_profiles(Namespace.CONDOR, key="+IgnoreSingularity", value="True")

        update_repo = Transformation(
            "update-repo",
            site="local",
            pfn=self.BASE_PATH / "bin/update-repo",
            is_stageable=True,
        )

        self.tc.add_transformations(apptainer_build, docker_pull, update_repo)

    def generate_sc(self):
        """Generate the Site Catalog."""
        
        username = getpass.getuser()
        hostname = re.sub(r"\..*", "", socket.gethostname())
        osdf_local_base = f"/ospool/{hostname}/data/{username}"

        if not os.path.exists(osdf_local_base):
            print(f"Unable to determine local OSDF location. Tried {osdf_local_base}")
            sys.exit(1)

        local = Site("local").add_directories(
            Directory(Directory.SHARED_STORAGE, self.output_dir).add_file_servers(
                FileServer(f"file://{self.output_dir}", Operation.ALL)
            ),
            Directory(Directory.SHARED_SCRATCH, self.scratch_dir).add_file_servers(
                FileServer(f"file://{self.scratch_dir}", Operation.ALL)
            ),
        )

        condorpool = Site("condorpool").add_pegasus_profile(style="condor")

        osdf = Site("osdf").add_directories(
            Directory(Directory.SHARED_SCRATCH, f"{osdf_local_base}/staging").add_file_servers(
                FileServer(f"osdf://{osdf_local_base}/staging", Operation.ALL)
            )
        )

        self.sc.add_sites(local, condorpool, osdf)

    def generate_workflow(self):
        """Generate the workflow."""
        
        with open("../apptainer-sync.yaml", "r") as file:
            self._conf = yaml.safe_load(file)

        print(f"{len(self._conf)} images ready for processing")

        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y%m%dT%H%M%SZ")

        for img in self._conf:
            img.setdefault("arch", ["x86_64"])
            cores, memory, disk = 4, 12000, 60000

            for arch in img["arch"]:
                singarch = "arm64" if arch == "aarch64" else "amd64"
                requirements = (
                    'GLIDECLIENT_Group == "main-container" && OSG_GLIDEIN_VERSION >= 825 && PelicanPluginVersion =!= undefined'
                )
                if arch == "aarch64":
                    requirements += ' && Arch == "aarch64"'

                tar = File(f"{img['name']}-{arch}.tar.gz")
                final_sif = f"{ts}.sif"

                if "apptainer_def" in img:
                    build_job = Job("apptainer-build").add_args(
                        img["name"], arch, singarch, img["apptainer_def"], final_sif, tar
                    ).add_outputs(tar, stage_out=False) \
                     .add_selector_profile(execution_site="local")
                elif "docker_image" in img:
                    build_job = Job("docker-pull").add_args(
                        img["name"], arch, singarch, img["docker_image"], final_sif, tar
                    ).add_outputs(tar, stage_out=False) \
                     .add_pegasus_profiles(cores=cores, memory=memory, diskspace=disk) \
                     .add_profiles(Namespace.CONDOR, key="Requirements", value=requirements)

                self.wf.add_jobs(build_job)

                outlog = File(f"{img['name']}-{arch}.log")
                repo_job = Job("update-repo").add_args(
                    self.REPO_DIR, img["name"], arch, final_sif, tar
                ).add_inputs(tar).add_outputs(outlog, stage_out=True).add_selector_profile(
                    execution_site="local"
                )
                self.wf.add_jobs(repo_job)

    def plan_workflow(self):
        """Plan and submit the workflow."""
        try:
            self.wf.plan(
                dir=str(self.runs_dir),
                output_dir=str(self.output_dir),
                sites=["condorpool"],
                staging_sites={"condorpool": "osdf"},
                submit=True,
            )
        except PegasusClientError as e:
            print(e.output)

    def __call__(self):
        """Run the workflow generation and planning."""
        self.generate_props()
        self.generate_tc()
        self.generate_sc()
        self.generate_workflow()
        self.plan_workflow()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Ensure no other instance is running
    schedd = htcondor.Schedd()
    ads = schedd.query(
        constraint='pegasus_wf_name == "osg-apptainer-sync-0"',
        projection=["ClusterId", "ProcId", "JobStatus"],
    )
    if ads:
        print("Another instance is already running. Exiting...")
        sys.exit(0)

    wf = OSGSIFSync()
    wf()
