import os
import sys
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Generator, Iterator, List, NoReturn, Optional, Sequence, Set, Tuple, cast

from cachetools import cached
from packaging.requirements import Requirement
from pyproject_api import BackendFailed, CmdStatus, Frontend

from tox.config.sets import EnvConfigSet
from tox.execute.api import ExecuteStatus
from tox.execute.pep517_backend import LocalSubProcessPep517Executor
from tox.execute.request import StdinSource
from tox.plugin import impl
from tox.tox_env.api import ToxEnvCreateArgs
from tox.tox_env.errors import Fail
from tox.tox_env.package import Package, PackageToxEnv
from tox.tox_env.python.package import DevLegacyPackage, PythonPackageToxEnv, SdistPackage, WheelPackage
from tox.tox_env.register import ToxEnvRegister
from tox.tox_env.runner import RunToxEnv

from ..api import VirtualEnv
from .util import dependencies_with_extras

if sys.version_info >= (3, 8):  # pragma: no cover (py38+)
    from importlib.metadata import Distribution, PathDistribution  # type: ignore[attr-defined]
else:  # pragma: no cover (<py38)
    from importlib_metadata import Distribution, PathDistribution
ConfigSettings = Optional[Dict[str, Any]]


class ToxBackendFailed(Fail, BackendFailed):
    def __init__(self, backend_failed: BackendFailed) -> None:
        Fail.__init__(self)
        result: Dict[str, Any] = {
            "code": backend_failed.code,
            "exc_type": backend_failed.exc_type,
            "exc_msg": backend_failed.exc_msg,
        }
        BackendFailed.__init__(
            self,
            result,
            backend_failed.out,
            backend_failed.err,
        )


class ToxCmdStatus(CmdStatus):
    def __init__(self, execute_status: ExecuteStatus) -> None:
        self._execute_status = execute_status

    @property
    def done(self) -> bool:
        # 1. process died
        status = self._execute_status
        if status.exit_code is not None:  # pragma: no branch
            return True  # pragma: no cover
        # 2. the backend output reported back that our command is done
        return b"\n" in status.out.rpartition(b"Backend: Wrote response ")[0]

    def out_err(self) -> Tuple[str, str]:
        status = self._execute_status
        if status is None or status.outcome is None:  # interrupt before status create # pragma: no branch
            return "", ""  # pragma: no cover
        return status.outcome.out_err()


class Pep517VirtualEnvPackager(PythonPackageToxEnv, VirtualEnv):
    """local file system python virtual environment via the virtualenv package"""

    def __init__(self, create_args: ToxEnvCreateArgs) -> None:
        super().__init__(create_args)
        self.root: Path = self.conf["package_root"]
        self._frontend_private: Optional[Pep517VirtualEnvFrontend] = None
        self.builds: Set[str] = set()
        self._distribution_meta: Optional[PathDistribution] = None
        self._package_dependencies: Optional[List[Requirement]] = None
        self._pkg_lock = RLock()  # can build only one package at a time

    @staticmethod
    def id() -> str:
        return "virtualenv-pep-517"

    @property
    def _frontend(self) -> "Pep517VirtualEnvFrontend":
        if self._frontend_private is None:
            self._frontend_private = Pep517VirtualEnvFrontend(self.root, self)
        return self._frontend_private

    def register_config(self) -> None:
        super().register_config()
        self.conf.add_config(
            keys=["meta_dir"],
            of_type=Path,
            default=lambda conf, name: self.env_dir / ".meta",
            desc="directory where to put the project metadata files",
        )
        self.conf.add_config(
            keys=["pkg_dir"],
            of_type=Path,
            default=lambda conf, name: self.env_dir / "dist",
            desc="directory where to put project packages",
        )

    @property
    def pkg_dir(self) -> Path:
        return cast(Path, self.conf["pkg_dir"])

    @property
    def meta_folder(self) -> Path:
        meta_folder: Path = self.conf["meta_dir"]
        meta_folder.mkdir(exist_ok=True)
        return meta_folder

    def register_run_env(self, run_env: RunToxEnv) -> Generator[Tuple[str, str], PackageToxEnv, None]:
        yield from super().register_run_env(run_env)
        self.builds.add(run_env.conf["package"])

    def _setup_env(self) -> None:
        super()._setup_env()
        if "wheel" in self.builds:
            build_requires = self._frontend.get_requires_for_build_wheel().requires
            self.installer.install(build_requires, PythonPackageToxEnv.__name__, "requires_for_build_wheel")
        if "sdist" in self.builds or "external" in self.builds:
            build_requires = self._frontend.get_requires_for_build_sdist().requires
            self.installer.install(build_requires, PythonPackageToxEnv.__name__, "requires_for_build_sdist")

    def _teardown(self) -> None:
        executor = self._frontend.backend_executor
        if executor is not None:  # pragma: no branch
            try:
                if executor.is_alive:
                    self._frontend._send("_exit")  # try first on amicable shutdown
            except SystemExit:  # pragma: no cover  # if already has been interrupted ignore
                pass
            finally:
                executor.close()
        super()._teardown()

    def perform_packaging(self, for_env: EnvConfigSet) -> List[Package]:
        """build the package to install"""
        of_type: str = for_env["package"]

        reqs: Optional[List[Requirement]] = None
        if of_type == "wheel":
            w_env = self._wheel_build_envs.get(for_env["wheel_build_env"])
            if w_env is not None and w_env is not self:
                with w_env.display_context(self._has_display_suspended):
                    reqs = w_env.get_package_dependencies() if isinstance(w_env, Pep517VirtualEnvPackager) else []
        if reqs is None:
            reqs = self.get_package_dependencies()

        extras: Set[str] = for_env["extras"]
        deps = dependencies_with_extras(reqs, extras)
        if of_type == "dev-legacy":
            deps = [*self.requires(), *self._frontend.get_requires_for_build_sdist().requires] + deps
            package: Package = DevLegacyPackage(self.core["tox_root"], deps)  # the folder itself is the package
        elif of_type == "sdist":
            with self._pkg_lock:
                package = SdistPackage(self._frontend.build_sdist(sdist_directory=self.pkg_dir).sdist, deps)
        elif of_type == "wheel":
            w_env = self._wheel_build_envs.get(for_env["wheel_build_env"])
            if w_env is not None and w_env is not self:
                with w_env.display_context(self._has_display_suspended):
                    return w_env.perform_packaging(for_env)
            else:
                with self._pkg_lock:
                    path = self._frontend.build_wheel(
                        wheel_directory=self.pkg_dir,
                        metadata_directory=self.meta_folder,
                        config_settings=self._wheel_config_settings,
                    ).wheel
                package = WheelPackage(path, deps)
        else:  # pragma: no cover # for when we introduce new packaging types and don't implement
            raise TypeError(f"cannot handle package type {of_type}")  # pragma: no cover
        return [package]

    def get_package_dependencies(self) -> List[Requirement]:
        with self._pkg_lock:
            if self._package_dependencies is None:  # pragma: no branch
                self._ensure_meta_present()
                requires: List[str] = cast(PathDistribution, self._distribution_meta).requires or []
                self._package_dependencies = [Requirement(i) for i in requires]  # pragma: no branch
        return self._package_dependencies

    def _ensure_meta_present(self) -> None:
        if self._distribution_meta is not None:  # pragma: no branch
            return  # pragma: no cover
        self.setup()
        dist_info = self._frontend.prepare_metadata_for_build_wheel(
            self.meta_folder,
            self._wheel_config_settings,
        ).metadata
        self._distribution_meta = Distribution.at(str(dist_info))  # type: ignore[no-untyped-call]

    @property
    def _wheel_config_settings(self) -> Optional[ConfigSettings]:
        return {"--global-option": ["--bdist-dir", str(self.env_dir / "build")]}

    def requires(self) -> Tuple[Requirement, ...]:
        return self._frontend.requires


class Pep517VirtualEnvFrontend(Frontend):
    def __init__(self, root: Path, env: Pep517VirtualEnvPackager) -> None:
        super().__init__(*Frontend.create_args_from_folder(root))
        self._tox_env = env
        self._backend_executor_: Optional[LocalSubProcessPep517Executor] = None
        into: Dict[str, Any] = {}
        pkg_cache = cached(into, key=lambda *args, **kwargs: "wheel" if "wheel_directory" in kwargs else "sdist")
        self.build_wheel = pkg_cache(self.build_wheel)  # type: ignore
        self.build_sdist = pkg_cache(self.build_sdist)  # type: ignore

    @property
    def backend_cmd(self) -> Sequence[str]:
        return ["python"] + self.backend_args

    def _send(self, cmd: str, **kwargs: Any) -> Tuple[Any, str, str]:
        try:
            if cmd == "prepare_metadata_for_build_wheel":
                # given we'll build a wheel we might skip the prepare step
                if "wheel" in self._tox_env.builds:
                    result = {
                        "code": 1,
                        "exc_type": "AvoidRedundant",
                        "exc_msg": "will need to build wheel either way, avoid prepare",
                    }
                    raise BackendFailed(result, "", "")
            return super()._send(cmd, **kwargs)
        except BackendFailed as exception:
            raise exception if isinstance(exception, ToxBackendFailed) else ToxBackendFailed(exception) from exception

    @contextmanager
    def _send_msg(
        self,
        cmd: str,
        result_file: Path,  # noqa: U100
        msg: str,  # noqa: U100
    ) -> Iterator[ToxCmdStatus]:  # type: ignore[override]
        with self._tox_env.execute_async(
            cmd=self.backend_cmd,
            cwd=self._root,
            stdin=StdinSource.API,
            show=None,
            run_id=cmd,
            executor=self.backend_executor,
        ) as execute_status:
            execute_status.write_stdin(f"{msg}{os.linesep}")
            yield ToxCmdStatus(execute_status)
        outcome = execute_status.outcome
        if outcome is not None:  # pragma: no branch
            outcome.assert_success()

    def _unexpected_response(self, cmd: str, got: Any, expected_type: Any, out: str, err: str) -> NoReturn:
        try:
            super()._unexpected_response(cmd, got, expected_type, out, err)
        except BackendFailed as exception:
            raise exception if isinstance(exception, ToxBackendFailed) else ToxBackendFailed(exception) from exception

    @property
    def backend_executor(self) -> LocalSubProcessPep517Executor:
        if self._backend_executor_ is None:
            environment_variables = self._tox_env.environment_variables.copy()
            backend = os.pathsep.join(str(i) for i in self._backend_paths).strip()
            if backend:
                environment_variables["PYTHONPATH"] = backend
            self._backend_executor_ = LocalSubProcessPep517Executor(
                colored=self._tox_env.options.is_colored,
                cmd=self.backend_cmd,
                env=environment_variables,
                cwd=self._root,
            )

        return self._backend_executor_

    @contextmanager
    def _wheel_directory(self) -> Iterator[Path]:
        yield self._tox_env.pkg_dir  # use our local wheel directory for building wheel


@impl
def tox_register_tox_env(register: ToxEnvRegister) -> None:
    register.add_package_env(Pep517VirtualEnvPackager)