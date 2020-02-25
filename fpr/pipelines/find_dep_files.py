import argparse
import asyncio
from dataclasses import asdict, dataclass
import functools
import logging
import pathlib
from random import randrange
from typing import Any, Tuple, Dict, Generator, AsyncGenerator, Union

from fpr.rx_util import on_next_save_to_jsonl
from fpr.serialize_util import get_in, extract_fields, iter_jsonlines
import fpr.docker.containers as containers
import fpr.docker.volumes as volumes
from fpr.models.pipeline import Pipeline
from fpr.models.org_repo import OrgRepo
from fpr.models.git_ref import GitRef
from fpr.models.pipeline import add_infile_and_outfile, add_volume_args
from fpr.models.language import dependency_file_patterns, DependencyFile
from fpr.pipelines.util import exc_to_str

log = logging.getLogger("fpr.pipelines.find_dep_files")

__doc__ = """
Given a repo_url, clones the repo, lists git refs for each tag
"""


@dataclass
class FindDepFilesBuildArgs:
    base_image_name: str = "debian"
    base_image_tag: str = "buster-slim"

    # NB: for buster variants a ripgrep package is available
    _DOCKERFILE = """
FROM {0.base_image}
RUN apt-get -y update && apt-get install -y curl git
RUN curl -LO https://github.com/BurntSushi/ripgrep/releases/download/11.0.2/ripgrep_11.0.2_amd64.deb
RUN dpkg -i ripgrep_11.0.2_amd64.deb
CMD ["bash", "-c"]
"""

    repo_tag = "dep-obs/find-dep-files"

    @property
    def base_image(self) -> str:
        return f"{self.base_image_name}:{self.base_image_tag}"

    @property
    def dockerfile(self) -> bytes:
        return FindDepFilesBuildArgs._DOCKERFILE.format(self).encode("utf-8")


async def build_container(args: FindDepFilesBuildArgs = None) -> str:
    # NB: can shell out to docker build if this doesn't work
    if args is None:
        args = FindDepFilesBuildArgs()

    await containers.build(args.dockerfile, args.repo_tag, pull=True)
    log.info(f"image built and successfully tagged {args.repo_tag}")
    return args.repo_tag


def parse_args(pipeline_parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser = add_infile_and_outfile(pipeline_parser)
    parser = add_volume_args(parser)
    parser.add_argument(
        "--glob",
        type=str,
        action="append",
        required=False,
        default=list(dependency_file_patterns.keys()),
        help=f"manifest globs to search for dep files in the repo. "
        f"Defaults to: {list(dependency_file_patterns.keys())}",
    )
    return parser


async def run_find_dep_files(
    item: Tuple[OrgRepo, GitRef], args: argparse.Namespace
) -> AsyncGenerator[Dict, None]:
    org_repo, git_ref = item
    log.debug(
        f"running find-dep-files on repo {org_repo.github_clone_url!r} ref {git_ref!r}"
    )
    name = f"dep-obs-find-dep-files-{org_repo.org}-{org_repo.repo}-{hex(randrange(1 << 32))[2:]}"

    async with containers.run(
        "dep-obs/find-dep-files:latest",
        name=name,
        cmd="/bin/bash",
        volumes=[
            volumes.DockerVolumeConfig(
                name=f"fpr-org_{org_repo.org}-repo_{org_repo.repo}",
                mount_point="/repos",
                labels=asdict(org_repo),
                delete=not args.keep_volumes,
            )
        ]
        if args.use_volumes
        else [],
    ) as c:
        if not args.use_volumes:
            await c.run("mkdir -p /repos", wait=True, check=True)
        await containers.ensure_repo(
            c, org_repo.github_clone_url, working_dir="/repos/"
        )
        await containers.ensure_ref(c, git_ref, working_dir="/repos/repo")
        branch, commit, tag, ripgrep_version = await asyncio.gather(
            containers.get_branch(c, working_dir="/repos/repo"),
            containers.get_commit(c, working_dir="/repos/repo"),
            containers.get_tag(c, working_dir="/repos/repo"),
            containers.get_ripgrep_version(c, working_dir="/repos/repo"),
        )
        log.debug(f"{name} stdout: {await c.log(stdout=True)}")
        log.debug(f"{name} stderr: {await c.log(stderr=True)}")

        for dep_file_path in await containers.find_files(
            args.glob, c, working_dir="/repos/repo"
        ):
            log.info(f"{c['Name']} found dep file: {dep_file_path}")
            yield dict(
                org=org_repo.org,
                repo=org_repo.repo,
                ref=git_ref.to_dict(),
                repo_url=org_repo.github_clone_url,
                commit=commit,
                branch=branch,
                tag=tag,
                versions={"ripgrep": ripgrep_version},
                dependency_file=DependencyFile.from_dict(
                    dict(
                        path=dep_file_path,
                        sha256=await containers.sha256sum(
                            c, dep_file_path, working_dir="/repos/repo"
                        )
                        or "",
                    )
                ).to_dict(),
            )


async def run_pipeline(
    source: Generator[Dict[str, Any], None, None], args: argparse.Namespace
) -> AsyncGenerator[Dict[str, Any], None]:
    log.info(f"started pipeline {pipeline.name} with globs: {args.glob}")

    try:
        await build_container()
    except Exception as e:
        log.error(
            f"error occurred building the find git refs image: {e}\n{exc_to_str()}"
        )

    for item in source:
        org_repo, git_ref = (
            OrgRepo.from_github_repo_url(item["repo_url"]),
            GitRef.from_dict(item["ref"]),
        )
        log.debug(f"finding dep files for {org_repo} {git_ref}")
        try:
            async for dep_file in run_find_dep_files((org_repo, git_ref), args):
                yield dep_file
        except Exception as e:
            log.error(f"error running find_dep_files:\n{exc_to_str()}")


# fields and types for the input and output JSON
IN_FIELDS: Dict[str, Union[type, str, Dict[str, str]]] = {
    "repo_url": str,
    **asdict(
        OrgRepo.from_github_repo_url(
            "https://github.com/mozilla-services/syncstorage-rs.git"
        )
    ),
    **{"ref": GitRef.from_dict(dict(value="dummy", kind="tag")).to_dict()},
}
OUT_FIELDS: Dict[str, Union[type, str, Dict[str, str]]] = {
    **IN_FIELDS,
    **{"dependency_file": DependencyFile(path=pathlib.Path("./"), sha256="").to_dict()},
}

pipeline = Pipeline(
    name="find_dep_files",
    desc=__doc__,
    fields=set(OUT_FIELDS.keys()),
    argparser=parse_args,
    reader=iter_jsonlines,
    runner=run_pipeline,
    writer=on_next_save_to_jsonl,
)
