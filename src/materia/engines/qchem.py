from __future__ import annotations
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import ast
import cclib
import copy
import functools
import materia as mtr
import numpy as np
import os
import re
import scipy.spatial
import shlex
import subprocess

from materia.utils import memoize
from materia.workflow import Workflow
from .engine import Engine
from ..tasks import ExternalTask, Task

__all__ = ["QChem", "QChemInput", "QChemOutput"]


class QChemInput:
    def __init__(
        self,
        *molecules: mtr.Molecule,
        settings: mtr.Settings,
        charges: Optional[Iterable[int]] = None,
        multiplicities: Optional[Iterable[int]] = None,
        total_charge: Optional[int] = None,
        total_multiplicity: Optional[int] = None,
        string: Optional[str] = None,
    ) -> None:
        self.molecules = molecules
        self.settings = settings
        self.charges = charges or [m.charge for m in self.molecules]
        self.multiplicities = multiplicities or [m.multiplicity for m in self.molecules]
        # FIXME: seems more rigorously correct to
        # determine the total charge and multiplicity
        # from combined structures rather than just
        # adding individual charges & multiplicities
        self.total_charge = total_charge or sum(self.charges)
        self.total_multiplicity = total_multiplicity or self.total_charge % 2 + 1
        self.string = string

    def write(self, filepath: str) -> None:
        with open(mtr.expand(filepath), "w") as f:
            f.write(str(self))

    def __str__(self) -> str:
        if self.string is not None:
            return self.string

        total_block = (
            f"  {self.total_charge} {self.total_multiplicity}\n--\n"
            if len(self.molecules) > 1
            else ""
        )

        molecule_block = total_block + "\n--\n".join(
            _molecule_to_structure_block(molecule=m, charge=c, multiplicity=mult)
            for m, c, mult in zip(self.molecules, self.charges, self.multiplicities)
        )

        if len(molecule_block) == 0:
            molecule_block = "  read"

        return f"$molecule\n{molecule_block}\n$end\n\n" + "\n".join(
            _block_to_str(block_name=block_name, block_params=block_params)
            for block_name, block_params in self.settings.d.items()
        )

    def __add__(self, other: mtr.QChemInput) -> str:
        return QChemInput(
            string=str(self) + "\n@@@\n\n" + str(other), settings=mtr.Settings()
        )


def _molecule_to_structure_block(
    molecule: mtr.Molecule,
    charge: Optional[int] = None,
    multiplicity: Optional[int] = None,
    mm_parameters: Optional[Dict[mtr.Atom, str]] = None,
    bonds: Optional[Dict[int, int]] = None,
) -> None:
    charge = charge or molecule.charge
    multiplicity = multiplicity or molecule.multiplicity
    mm_parameters = mm_parameters or {}
    bonds = bonds or {}

    return (
        f"  {charge or molecule.charge} {multiplicity or molecule.multiplicity}\n"
        + "\n".join(
            (
                f"  {a.atomic_symbol}  "
                + "  ".join(
                    str(p)
                    for p in a.position.reshape(
                        3,
                    )
                )
                + (f" {mm_parameters[a]}" if a in mm_parameters else "")
                + (
                    " "
                    + "  ".join(
                        str(j + 1) for j in bonds[i] + [-1] * (4 - len(bonds[i]))
                    )
                    if i in bonds
                    else ""
                )
                for i, a in enumerate(molecule.structure.atoms)
            )
        )
    )


def _block_to_str(
    block_name: str, block_params: Union[List[Any], Dict[str, Any]]
) -> str:
    if block_name == "xc_functional":
        return _xc_functional_str(*block_params)
    else:
        return _block_str(block_name=block_name, **block_params)


def _block_str(
    block_name: str, **block_params: Union[List[Any], Dict[str, Any]]
) -> str:
    longest_key_length = max(len(k) for k in block_params)
    return (
        f"${block_name}\n"
        + "".join(
            f"  {k}" + " " * (longest_key_length - len(k) + 1) + f"{v}\n"
            for k, v in block_params.items()
        )
        + "$end\n"
    )


def _xc_functional_str(*functional_tuples: Tuple[str, str, str]) -> str:
    component_types, component_names, component_coefficients = zip(*functional_tuples)
    longest_component_name = max(len(cn) for cn in component_names)

    s = "$xc_functional\n"
    for ct, cn, cc in zip(component_types, component_names, component_coefficients):
        if ct == "K":
            s += "  K " + " " * (longest_component_name + 1) + f" {cc}\n"
        else:
            s += (
                f"  {ct} {cn}"
                + " " * (longest_component_name - len(cn) + 1)
                + f" {cc}\n"
            )
    s += "$end\n"

    return s


# --------------------------- OUTPUT ----------------------------- #


class QChemOutput:
    def __init__(self, filepath: str) -> None:
        self.filepath = mtr.expand(filepath)

    @property
    @memoize
    def cclib_out(self):
        return cclib.io.ccread(self.filepath)

    @property
    def footer(
        self,
    ) -> Dict[mtr.Quantity, mtr.Quantity, Tuple[int, str, int, int, int, int, str]]:
        with open(self.filepath, "r") as f:
            lines = "".join(f.readlines())
        s = r"""\s*Total\s*job\s*time\s*:\s*(\d*\.\d*)
                \s*s\s*\(\s*wall\s*\)\s*,\s*(\d*\.\d*)
                \s*s\s*\(\s*cpu\s*\)\s*(\w*)\s*(\w*)
                \s*(\d*)\s*(\d*)\s*:\s*(\d*)\s*:\s*
                (\d*)\s*(\d*)\s*"""
        pattern = re.compile(s)
        (
            walltime,
            cputime,
            day,
            month,
            date,
            hour,
            minutes,
            seconds,
            year,
        ) = pattern.search(lines).groups()

        walltime = float(walltime) * mtr.second
        cputime = float(cputime) * mtr.second
        date = (int(year), month, int(date), int(hour), int(minutes), int(seconds), day)

        return {"walltime": walltime, "cputime": cputime, "date": date}

    @property
    def frontier_energies(self) -> Dict[mtr.Quantity, mtr.Quantity]:
        moenergies = self.cclib_out.moenergies
        homo_indices = self.cclib_out.homos

        homos = []
        lumos = []

        for moe, h in zip(moenergies, homo_indices):
            homo, lumo = moe[h : h + 2]
            homos.append(homo)
            lumos.append(lumo)

        homo = max(homos) * mtr.eV
        lumo = min(lumos) * mtr.eV

        return {"homo": homo, "lumo": lumo}

    @property
    def polarizability(self) -> mtr.Polarizability:
        *_, pol = self.cclib_out.polarizabilities

        return mtr.Polarizability(polarizability_tensor=pol * mtr.au_volume)

    @property
    def structure(self) -> mtr.Structure:
        coords = self.cclib_out.atomcoords[-1, :, :] * mtr.angstrom
        zs = self.cclib_out.atomnos

        atoms = (mtr.Atom(element=Z, position=p) for Z, p in zip(zs, coords))

        return mtr.Structure(*atoms)

    @property
    def electronic_excitations(self) -> mtr.ExcitationSpectrum:
        engs = self.cclib_out.etenergies / mtr.cm
        engs = (mtr.h * mtr.c * engs).convert(mtr.eV)
        excitations = tuple(
            mtr.Excitation(
                energy=eng, oscillator_strength=osc, symmetry=sym, contributions=cont
            )
            for eng, osc, sym, cont in zip(
                engs,
                self.cclib_out.etoscs,
                self.cclib_out.etsyms,
                self.cclib_out.etsecs,
            )
        )
        return mtr.ExcitationSpectrum(excitations)

    @property
    def total_energy(self) -> mtr.Quantity:
        return self.cclib_out.scfenergies * mtr.eV


# ------------------------ ENGINE -------------------------- #


class QChem(Engine):
    def __init__(
        self,
        executable: Optional[str] = "qchem",
        scratch_dir: Optional[str] = None,
        qcenv: Optional[str] = None,
        num_processors: Optional[int] = None,
        num_threads: Optional[int] = None,
        arguments: Optional[Iterable[str]] = None,
        save: Optional[bool] = False,
        savename: Optional[str] = None,
    ) -> None:
        self.scratch_dir = mtr.expand(scratch_dir) if scratch_dir is not None else None
        self.qcenv = shlex.quote(mtr.expand(qcenv)) if qcenv is not None else None
        self.save = save
        self.savename = savename
        super().__init__(
            executable=executable,
            num_processors=num_processors,
            num_threads=num_threads,
            arguments=arguments,
        )

    def env(self) -> Dict[str, str]:
        if self.scratch_dir is None and self.qcenv is None:
            return None

        if self.qcenv is not None:
            # FIXME: shell=True needs to be avoided!!
            d = ast.literal_eval(
                re.match(
                    r"environ\((.*)\)",
                    subprocess.check_output(
                        f". {self.qcenv}; python -c 'import os; print(os.environ)'",
                        shell=True,
                    )
                    .decode()
                    .strip(),
                ).group(1)
            )
        else:
            d = {}

        if self.scratch_dir is not None:
            d["QCSCRATCH"] = mtr.expand(self.scratch_dir)

        return d

    def command(
        self,
        inp: str,
        out: str,
        work_dir: str,
        arguments: Optional[Iterable[str]] = None,
    ) -> str:
        cmd = [self.executable]
        if self.save:
            cmd.append("-save")
        if self.arguments is not None:
            cmd.extend(list(self.arguments) + list(arguments or []))
        if self.num_processors is not None:
            cmd.append(f"-np {self.num_processors}")
        if self.num_threads is not None:
            cmd.append(f"-nt {self.num_threads}")
        if self.save and self.savename:
            cmd.append(f" {self.savename}")

        # FIXME: shlex.quote should be used but it doesn't work...
        return shlex.split(" ".join(cmd + [inp, out]))

    def aimd(
        self,
        io: mtr.IO,
        handlers: Optional[Iterable[mtr.Handler]] = None,
        name: Optional[str] = None,
    ) -> QChemAIMD:
        return QChemAIMD(engine=self, io=io, handlers=handlers, name=name)

    def koopman_error(
        self,
        gs_io: mtr.IO,
        cation_io: mtr.IO,
        anion_io: mtr.IO,
        handlers: Optional[Iterable[mtr.Handler]] = None,
        name: Optional[str] = None,
    ) -> QChemKoopmanError:
        return QChemKoopmanError(
            engine=self,
            gs_io=gs_io,
            cation_io=cation_io,
            anion_io=anion_io,
            handlers=handlers,
            name=name,
        )

    # def koopman_error_lpscf(
    #     self,
    #     gs_io: mtr.IO,
    #     cation_io: mtr.IO,
    #     anion_io: mtr.IO,
    #     handlers: Optional[Iterable[mtr.Handler]] = None,
    #     name: Optional[str] = None,
    # ) -> QChemKoopmanErrorLPSCF:
    #     return QChemKoopmanErrorLPSCF(
    #         engine=self,
    #         gs_io=gs_io,
    #         cation_io=cation_io,
    #         anion_io=anion_io,
    #         handlers=handlers,
    #         name=name,
    #     )

    # def lpscf(
    #     self,
    #     io: mtr.IO,
    #     handlers: Optional[Iterable[mtr.Handler]] = None,
    #     name: Optional[str] = None,
    # ) -> QChemLPSCF:
    #     return QChemLPSCF(engine=self, io=io, handlers=handlers, name=name)

    # def lpscfrs(
    #     self,
    #     io: mtr.IO,
    #     handlers: Optional[Iterable[mtr.Handler]] = None,
    #     name: Optional[str] = None,
    # ) -> QChemLPSCFRS:
    #     return QChemLPSCFRS(engine=self, io=io, handlers=handlers, name=name)

    def lrtddft(
        self,
        io: mtr.IO,
        handlers: Optional[Iterable[mtr.Handler]] = None,
        name: Optional[str] = None,
    ) -> QChemLRTDDFT:
        return QChemLRTDDFT(engine=self, io=io, handlers=handlers, name=name)

    def minimize_koopman_error(
        self,
        io: mtr.IO,
        handlers: Optional[Iterable[mtr.Handler]] = None,
        name: Optional[str] = None,
    ) -> QChemMinimizeKoopmanError:
        return QChemMinimizeKoopmanError(
            engine=self, io=io, handlers=handlers, name=name
        )

    # def minimize_koopman_error_lpscf(
    #     self,
    #     io: mtr.IO,
    #     handlers: Optional[Iterable[mtr.Handler]] = None,
    #     name: Optional[str] = None,
    # ) -> QChemMinimizeKoopmanErrorLPSCF:
    #     return QChemMinimizeKoopmanErrorLPSCF(
    #         engine=self, io=io, handlers=handlers, name=name
    #     )

    def optimize(
        self,
        io: mtr.IO,
        handlers: Optional[Iterable[mtr.Handler]] = None,
        name: Optional[str] = None,
    ) -> QChemOptimize:
        return QChemOptimize(engine=self, io=io, handlers=handlers, name=name)

    def polarizability(
        self,
        io: mtr.IO,
        handlers: Optional[Iterable[mtr.Handler]] = None,
        name: Optional[str] = None,
    ) -> QChemPolarizability:
        return QChemPolarizability(engine=self, io=io, handlers=handlers, name=name)

    def single_point(
        self,
        io: mtr.IO,
        handlers: Optional[Iterable[mtr.Handler]] = None,
        name: Optional[str] = None,
    ) -> QChemSinglePoint:
        return QChemSinglePoint(engine=self, io=io, handlers=handlers, name=name)

    def single_point_frontier(
        self,
        io: mtr.IO,
        handlers: Optional[Iterable[mtr.Handler]] = None,
        name: Optional[str] = None,
    ) -> QChemSinglePointFrontier:
        return QChemSinglePointFrontier(
            engine=self, io=io, handlers=handlers, name=name
        )

    def volume(
        self,
        io: mtr.IO,
        handlers: Optional[Iterable[mtr.Handler]] = None,
        name: Optional[str] = None,
    ) -> QChemVolume:
        return QChemVolume(engine=self, io=io, handlers=handlers, name=name)


class QChemBaseTask(ExternalTask):
    def defaults(self, settings: mtr.Settings) -> mtr.Settings:
        raise NotImplementedError

    def parse(self, output: str) -> Any:
        raise NotImplementedError

    def compute(
        self,
        molecule: mtr.Molecule,
        settings: Optional[mtr.Settings] = None,
        arguments: Optional[Iterable[str]] = None,
    ) -> Any:
        s = mtr.Settings() if settings is None else copy.deepcopy(settings)

        inp = mtr.QChemInput(molecule, settings=self.defaults(s))

        with self.io() as io:
            inp.write(io.inp)

            self.engine.execute(self.io, arguments=arguments)

            return self.parse(io.out)


# class ExecuteQChem(QChemBaseTask):
#     def compute(self) -> Any:
#         with self.io() as io:
#             self.engine.execute(io.inp, io.out)


class QChemAIMD(QChemBaseTask):
    def parse(self, output: str) -> Any:
        with open(output, "r") as f:
            return "".join(f.readlines())

    def defaults(self, settings: mtr.Settings) -> mtr.Settings:
        if ("rem", "exchange") not in settings and (
            "rem",
            "method",
        ) not in settings:
            settings["rem", "exchange"] = "HF"
        if ("rem", "basis") not in settings:
            settings["rem", "basis"] = "3-21G"
        if ("rem", "jobtype") not in settings:
            settings["rem", "jobtype"] = "aimd"
        if ("rem", "time_step") not in settings:
            settings["rem", "time_step"] = 1
        if ("rem", "aimd_steps") not in settings:
            settings["rem", "aimd_steps"] = 10
        if ("velocity",) not in settings and (
            "rem",
            "aimd_init_veloc",
        ) not in settings:
            settings["rem", "aimd_init_veloc"] = "thermal"
        if (
            ("rem", "aimd_init_veloc") in settings
            and settings["rem", "aimd_init_veloc"].lower().strip() == "thermal"
            and ("rem", "aimd_temp") not in settings
        ):
            settings["rem", "aimd_temp"] = 300

        return settings


# class QChemLPSCF(QChemBaseTask):
#     def parse(self, output: str) -> Any:
#         try:
#             energy = cclib.io.ccread(output).scfenergies * mtr.eV
#         except AttributeError:
#             energy = None

#         return energy

#     def defaults(self, settings: mtr.Settings) -> mtr.Settings:
#         if ("rem", "exchange") not in settings and ("rem", "method",) not in settings:
#             settings["rem", "exchange"] = "HF"
#         if ("rem", "basis") not in settings:
#             settings["rem", "basis"] = "3-21G"
#         if ("rem", "jobtype") not in settings:
#             settings["rem", "jobtype"] = "sp"
#         if ("rem", "frgm_method") not in settings:
#             settings["rem", "frgm_method"] = "stoll"
#         if ("rem_frgm", "scf_convergence") not in settings:
#             settings["rem_frgm", "scf_convergence"] = 2
#         if ("rem_frgm", "thresh") not in settings:
#             settings["rem_frgm", "thresh"] = 5

#         return settings

#     def compute(
#         self, *fragments: mtr.Molecule, settings: Optional[mtr.Settings] = None,
#     ) -> Any:
#         s = mtr.Settings() if settings is None else copy.deepcopy(settings)

#         # FIXME: this is essentially a hotpatch to handle fragments
#         # come up with something more elegant/sensible ASAP
#         inp = mtr.QChemInput(*fragments, settings=self.defaults(s),)

#         with self.io() as io:
#             inp.write(io.inp)

#             self.engine.execute(self.io)

#             return self.parse(io.out)


# class QChemLPSCFRS(QChemBaseTask):
#     def defaults(self, settings: mtr.Settings) -> mtr.Settings:
#         if ("rem", "exchange") not in settings and ("rem", "method",) not in settings:
#             settings["rem", "exchange"] = "HF"
#         if ("rem", "basis") not in settings:
#             settings["rem", "basis"] = "3-21G"
#         if ("rem", "jobtype") not in settings:
#             settings["rem", "jobtype"] = "sp"
#         if ("rem", "frgm_method") not in settings:
#             settings["rem", "frgm_method"] = "stoll"
#         if ("rem_frgm", "scf_convergence") not in settings:
#             settings["rem_frgm", "scf_convergence"] = 2
#         if ("rem_frgm", "thresh") not in settings:
#             settings["rem_frgm", "thresh"] = 5
#         if ("rem_frgm", "frgm_lpcorr") not in settings:
#             settings["rem_frgm", "frgm_lpcorr"] = "rs_exact_scf"

#         return settings


class QChemKoopmanError(Task):
    def __init__(
        self,
        engine: mtr.Engine,
        gs_io: mtr.IO,
        cation_io: mtr.IO,
        anion_io: mtr.IO,
        handlers: Optional[Iterable[mtr.Handler]] = None,
        name: Optional[str] = None,
    ) -> None:
        super().__init__(
            handlers=handlers,
            name=name,
        )
        self.engine = engine
        self.gs_io = gs_io
        self.cation_io = cation_io
        self.anion_io = anion_io

    def defaults(self, settings: mtr.Settings) -> mtr.Settings:
        if ("rem", "exchange") not in settings and ("rem", "method") not in settings:
            settings["rem", "exchange"] = "hf"
        if ("rem", "basis") not in settings:
            settings["rem", "basis"] = "3-21G"
        if ("rem", "jobtype") not in settings:
            settings["rem", "jobtype"] = "sp"

        return settings

    def compute(
        self,
        molecule: mtr.Molecule,
        settings: Optional[mtr.Settings] = None,
    ) -> mtr.Quantity:
        s = mtr.Settings() if settings is None else copy.deepcopy(settings)
        input_settings = self.defaults(s)

        neutral_sp = self.engine.single_point_frontier(self.gs_io, name="neutral")
        cation_sp = self.engine.single_point(self.cation_io, name="cation")
        anion_sp = self.engine.single_point(self.anion_io, name="anion")

        cation = copy.deepcopy(molecule)
        cation.charge += 1
        cation.multiplicity = cation.multiplicity % 2 + 1

        anion = copy.deepcopy(molecule)
        anion.charge -= 1
        anion.multiplicity = anion.multiplicity % 2 + 1

        neutral_sp.requires(
            molecule=molecule,
            settings=input_settings,
        )
        cation_sp.requires(
            molecule=cation,
            settings=input_settings,
        )
        anion_sp.requires(
            molecule=anion,
            settings=input_settings,
        )

        wf = Workflow(neutral_sp, cation_sp, anion_sp)

        out = wf.compute()

        neutral, homo, lumo = out["neutral"]
        cation = out["cation"]
        anion = out["anion"]

        ea = neutral - anion
        ip = cation - neutral

        J_squared = (ip + homo) ** 2
        if ea > 0 * ea.unit:
            J_squared += (ea + lumo) ** 2

        return np.sqrt(J_squared.convert(mtr.eV ** 2).value.item()) * mtr.eV


# class QChemKoopmanErrorLPSCF(Task):
#     def __init__(
#         self,
#         engine: mtr.Engine,
#         gs_io: mtr.IO,
#         cation_io: mtr.IO,
#         anion_io: mtr.IO,
#         handlers: Optional[Iterable[mtr.Handler]] = None,
#         name: Optional[str] = None,
#     ) -> None:
#         super().__init__(
#             (engine.num_threads or 1) * (engine.num_processors or 1),
#             handlers=handlers,
#             name=name,
#         )
#         self.engine = engine
#         self.gs_io = gs_io
#         self.cation_io = cation_io
#         self.anion_io = anion_io

#     def defaults(self, settings: mtr.Settings) -> mtr.Settings:
#         if ("rem", "exchange") not in settings and ("rem", "method") not in settings:
#             settings["rem", "exchange"] = "hf"
#         if ("rem", "basis") not in settings:
#             settings["rem", "basis"] = "3-21G"
#         if ("rem", "jobtype") not in settings:
#             settings["rem", "jobtype"] = "sp"

#         return settings

#     def compute(
#         self,
#         *fragments: mtr.Structure,
#         active_fragment: int,
#         settings: Optional[mtr.Settings] = None,
#         num_consumers: Optional[int] = 1,
#     ) -> float:
#         s = mtr.Settings() if settings is None else copy.deepcopy(settings)

#         gs_charges = [0] * len(fragments)
#         gs_multiplicities = [1] * len(fragments)

#         cation_charges = gs_charges.copy()
#         cation_multiplicities = gs_multiplicities.copy()
#         cation_charges[active_fragment] += 1
#         cation_multiplicities[active_fragment] = 2

#         anion_charges = gs_charges.copy()
#         anion_multiplicities = gs_multiplicities.copy()
#         anion_charges[active_fragment] -= 1
#         anion_multiplicities[active_fragment] = 2

#         input_settings = self.defaults(s)

#         gs = self.engine.single_point_frontier(self.gs_io, name="gs")
#         gs_structure = mtr.QChemFragments(
#             fragments,
#             gs_charges,
#             gs_multiplicities,
#             total_charge=sum(gs_charges),
#             total_multiplicity=1,
#         )
#         gs.requires(structure=gs_structure, settings=input_settings)

#         cation = self.engine.single_point(self.cation_io, name="cation")
#         cation_structure = mtr.QChemFragments(
#             fragments,
#             cation_charges,
#             cation_multiplicities,
#             total_charge=sum(cation_charges),
#             total_multiplicity=2,
#         )
#         cation.requires(structure=cation_structure, settings=input_settings)

#         anion = self.engine.single_point(self.anion_io, name="anion")
#         anion_structure = mtr.QChemFragments(
#             fragments,
#             anion_charges,
#             anion_multiplicities,
#             total_charge=sum(anion_charges),
#             total_multiplicity=2,
#         )
#         anion.requires(structure=anion_structure, settings=input_settings)

#         wf = Workflow(gs, cation, anion)

#         out = wf.compute(available_cores=self.num_cores, num_consumers=num_consumers)

#         energy, homo, lumo = out["gs"]
#         cation = out["cation"]
#         anion = out["anion"]

#         ea = energy - anion
#         ip = cation - energy

#         J_squared = (ea + lumo) ** 2 + (ip + homo) ** 2

#         return np.sqrt(J_squared.convert(mtr.eV ** 2).value.item())


class QChemLRTDDFT(QChemBaseTask):
    def parse(self, output: str) -> Any:
        return mtr.QChemOutput(filepath=output).electronic_excitations

    def defaults(self, settings: mtr.Settings) -> mtr.Settings:
        if ("rem", "exchange") not in settings and (
            "rem",
            "method",
        ) not in settings:
            settings["rem", "exchange"] = "HF"
        if ("rem", "basis") not in settings:
            settings["rem", "basis"] = "3-21G"
        if ("rem", "cis_n_roots") not in settings:
            settings["rem", "cis_n_roots"] = 1
        if ("rem", "cis_singlets") not in settings:
            settings["rem", "cis_singlets"] = True
        if ("rem", "cis_triplets") not in settings:
            settings["rem", "cis_triplets"] = False
        if ("rem", "rpa") not in settings:
            settings["rem", "rpa"] = True

        return settings

    def compute(
        self,
        molecule: mtr.Molecule,
        settings: Optional[mtr.Settings] = None,
        num_nto_pairs: Optional[int] = 0,
        n_x: Optional[int] = 50,
        n_y: Optional[int] = 50,
        n_z: Optional[int] = 50,
    ) -> mtr.Molecule:
        s = mtr.Settings() if settings is None else copy.deepcopy(settings)

        inp = mtr.QChemInput(molecule, settings=self.defaults(s))

        if num_nto_pairs > 0:
            inp += _nto_pairs(molecule, s, num_nto_pairs, n_x, n_y, n_z)

        with self.io() as io:
            inp.write(io.inp)

            self.engine.execute(self.io)

            molecule.electronic_excitations = self.parse(io.out)

        return molecule


class QChemMinimizeKoopmanError(Task):
    def __init__(
        self,
        engine: mtr.Engine,
        io: mtr.IO,
        handlers: Optional[Iterable[mtr.Handler]] = None,
        name: Optional[str] = None,
    ) -> None:
        super().__init__(
            handlers=handlers,
            name=name,
        )
        self.engine = engine
        self.io = io

    def defaults(self, settings: mtr.Settings) -> mtr.Settings:
        if ("rem", "basis") not in settings:
            settings["rem", "basis"] = "3-21G"
        if ("rem", "jobtype") not in settings:
            settings["rem", "jobtype"] = "sp"
        if ("rem", "exchange") not in settings:
            settings["rem", "exchange"] = "gen"
        if ("rem", "lrc_dft") not in settings:
            settings["rem", "lrc_dft"] = True
        if ("rem", "src_dft") not in settings:
            settings["rem", "src_dft"] = 2

        return settings

    def compute(
        self,
        molecule: mtr.Molecule,
        settings: Optional[mtr.Settings] = None,
        epsilon: Optional[Union[int, float]] = 1.0,
        alpha: Optional[float] = None,
        num_evals: Optional[int] = 5,
    ) -> Tuple[float, float, mtr.Quantity]:
        def _objective(omega: float, _alpha: float) -> float:
            beta = 1 / epsilon - _alpha
            s = mtr.Settings() if settings is None else copy.deepcopy(settings)
            s = self.defaults(s)
            s["rem", "hf_sr"] = int(round(1000 * _alpha))
            s["rem", "hf_lr"] = int(1000 / epsilon)
            s["xc_functional"] = (
                ("X", "HF", _alpha),
                ("X", "wPBE", beta),
                ("X", "PBE", 1 - _alpha - beta),
                ("C", "PBE", 1.0),
            )
            omega = int(round(1000 * omega))
            s["rem", "omega"] = s["rem", "omega2"] = omega

            wd = mtr.expand(f"{io.work_dir}/{omega}")

            gs_io = mtr.IO("gs.in", "gs.out", wd)
            cation_io = mtr.IO("cation.in", "cation.out", wd)
            anion_io = mtr.IO("anion.in", "anion.out", wd)

            ke = self.engine.koopman_error(gs_io, cation_io, anion_io)

            return ke.compute(molecule, s).value

        with self.io() as io:
            if alpha is None:
                [omega, alpha], J = mtr.MaxLIPOTR(_objective).compute(
                    x_min=[1e-3, 0], x_max=[1, 1 / epsilon], num_evals=num_evals
                )
            else:
                [omega], J = mtr.MaxLIPOTR(
                    functools.partial(_objective, _alpha=alpha)
                ).compute(x_min=1e-3, x_max=1, num_evals=num_evals)

        return omega, alpha, J * mtr.eV


# class QChemMinimizeKoopmanErrorLPSCF(Task):
#     def __init__(
#         self,
#         engine: mtr.Engine,
#         io: mtr.IO,
#         handlers: Optional[Iterable[mtr.Handler]] = None,
#         name: Optional[str] = None,
#     ) -> None:
#         super().__init__(
#             (engine.num_threads or 1) * (engine.num_processors or 1),
#             handlers=handlers,
#             name=name,
#         )
#         self.engine = engine
#         self.io = io

#     def defaults(self, settings: mtr.Settings) -> mtr.Settings:
#         if ("rem", "basis") not in settings:
#             settings["rem", "basis"] = "3-21G"
#         if ("rem", "jobtype") not in settings:
#             settings["rem", "jobtype"] = "sp"
#         if ("rem", "exchange") not in settings:
#             settings["rem", "exchange"] = "gen"
#         if ("rem", "lrc_dft") not in settings:
#             settings["rem", "lrc_dft"] = True
#         if ("rem", "src_dft") not in settings:
#             settings["rem", "src_dft"] = 2

#         return settings

#     def compute(
#         self,
#         *fragments: mtr.Structure,
#         active_fragment: int,
#         settings: Optional[mtr.Settings] = None,
#         epsilon: Optional[Union[int, float]] = 1.0,
#         alpha: Optional[float] = 0.2,
#         num_evals: Optional[int] = 5,
#     ) -> float:
#         beta = 1 / epsilon - alpha

#         s = self.defaults(settings)
#         s["rem", "hf_sr"] = int(round(1000 * alpha))
#         s["rem", "hf_lr"] = int(round(1000 * (alpha + beta)))
#         s["xc_functional"] = (
#             ("X", "HF", alpha),
#             ("X", "wPBE", beta),
#             ("X", "PBE", 1 - alpha - beta),
#             ("C", "PBE", 1.0),
#         )

#         with self.io() as io:

#             def f(omega):
#                 omega = int(round(1000 * omega))
#                 s["rem", "omega"] = s["rem", "omega2"] = omega

#                 wd = mtr.expand(f"{io.work_dir}/{omega}")

#                 gs_io = mtr.IO("gs.in", "gs.out", wd)
#                 cation_io = mtr.IO("cation.in", "cation.out", wd)
#                 anion_io = mtr.IO("anion.in", "anion.out", wd)

#                 ke = mtr.QChemKoopmanErrorLPSCF(
#                                       self.engine,
#                                       gs_io,
#                                       cation_io,
#                                       anion_io)
#                 # FIXME: not sure the best way to handle num_consumers here...
#                 return ke.compute(
#                     *fragments,
#                     active_fragment=active_fragment,
#                     settings=settings,
#                     num_consumers=3,
#                 )

#             return mtr.MaxLIPOTR(f).compute(x_min=1e-3, x_max=1, num_evals=num_evals)


class QChemOptimize(QChemBaseTask):
    def parse(self, output: str) -> Any:
        return QChemOutput(output).structure

    def defaults(self, settings: mtr.Settings) -> mtr.Settings:
        if ("rem", "exchange") not in settings and (
            "rem",
            "method",
        ) not in settings:
            settings["rem", "exchange"] = "HF"
        if ("rem", "basis") not in settings:
            settings["rem", "basis"] = "3-21G"
        if ("rem", "jobtype") not in settings:
            settings["rem", "jobtype"] = "opt"

        return settings

    def compute(
        self, molecule: mtr.Molecule, settings: Optional[mtr.Settings] = None
    ) -> mtr.Molecule:
        molecule.structure = super().compute(molecule, settings)
        return molecule


class QChemPolarizability(QChemBaseTask):
    def parse(self, output: str) -> Any:
        try:
            polarizability = (
                cclib.io.ccread(output).polarizabilities[-1] * mtr.au_volume
            )
        except AttributeError:
            polarizability = None

        return mtr.Polarizability(polarizability)

    def defaults(self, settings: mtr.Settings) -> mtr.Settings:
        if ("rem", "exchange") not in settings and (
            "rem",
            "method",
        ) not in settings:
            settings["rem", "exchange"] = "HF"
        if ("rem", "basis") not in settings:
            settings["rem", "basis"] = "3-21G"
        if ("rem", "jobtype") not in settings:
            settings["rem", "jobtype"] = "polarizability"

        return settings

    def compute(
        self, molecule: mtr.Molecule, settings: Optional[mtr.Settings] = None
    ) -> mtr.Molecule:
        # NOTE: bug workaround for parallel polarizability calculation in Q-Chem 5.2.1
        os.environ["QCINFILEBASE"] = "0"
        molecule.polarizability = super().compute(molecule, settings)
        return molecule


class QChemVolume(QChemBaseTask):
    def parse(self, output: str) -> Any:
        pat = re.compile(r"Tesselation\s*\.PQR\s*file")

        with open(output, "r") as f:
            lines = f.readlines()

        pqr_start, pqr_end = [i for i, l in enumerate(lines) if pat.search(l)]

        points = []

        for line in lines[pqr_start + 1 : pqr_end]:
            _, _, _, _, _, _, x, y, z, _, _ = line.strip().split()
            points.append([float(x), float(y), float(z)])

        points = np.array(points)

        # adapted from
        # https://stackoverflow.com/questions/24733185/volume-of-convex-hull-with-qhull-from-scipy

        ch = scipy.spatial.ConvexHull(points)
        simplices = np.column_stack(
            (np.repeat(ch.vertices[0], ch.nsimplex), ch.simplices)
        )
        tets = ch.points[simplices]

        return (
            np.sum(
                mtr.tetrahedron_volume(tets[:, 0], tets[:, 1], tets[:, 2], tets[:, 3])
            )
            * mtr.angstrom ** 3
        )

    def defaults(self, settings: mtr.Settings) -> mtr.Settings:
        if ("rem", "exchange") not in settings and (
            "rem",
            "method",
        ) not in settings:
            settings["rem", "exchange"] = "HF"
        if ("rem", "basis") not in settings:
            settings["rem", "basis"] = "STO-3G"
        if ("rem", "scf_convergence") not in settings:
            settings["rem", "scf_convergence"] = 1
        if ("rem", "solvent_method") not in settings:
            settings["rem", "solvent_method"] = "pcm"

        if ("pcm", "theory") not in settings:
            settings["pcm", "theory"] = "iefpcm"
        if ("pcm", "printlevel") not in settings:
            settings["pcm", "printlevel"] = 2
        if ("pcm", "heavypoints") not in settings:
            settings["pcm", "heavypoints"] = 194
        if ("pcm", "hpoints") not in settings:
            settings["pcm", "hpoints"] = 194

        if ("solvent", "dielectric") not in settings:
            settings["solvent", "dielectric"] = 1

        return settings

    def compute(
        self, molecule: mtr.Molecule, settings: Optional[mtr.Settings] = None
    ) -> mtr.Molecule:
        molecule.volume = super().compute(molecule, settings)
        return molecule


# class QChemRTTDDFT(Task):
#     def __init__(
#         self,
#         structure,
#         input_name,
#         output_name,
#         scratch_directory,
#         settings=None,
#         tdscf_settings=None,
#         executable="qchem",
#         work_directory=".",
#         num_cores=1,
#         parallel=False,
#         handlers=None,
#         name=None,
#     ):
#         super().__init__(handlers=handlers, name=name)
#         self.work_directory = mtr.expand(work_directory)
#         self.input_path = mtr.expand(os.path.join(work_directory, input_name))
#         self.output_path = mtr.expand(os.path.join(work_directory, output_name))
#         self.scratch_directory = mtr.expand(scratch_directory)
#         self.executable = executable
#         self.num_cores = num_cores
#         self.parallel = parallel
#         try:
#             os.makedirs(mtr.expand(work_directory))
#         except FileExistsError:
#             pass
#         settings = settings or mtr.Settings()
#         settings["molecule", "structure"] = structure
#         if ("rem", "exchange") not in settings and ("rem", "method",) not in settings:
#             settings["rem", "exchange"] = "HF"
#         if ("rem", "basis") not in settings:
#             settings["rem", "basis"] = "3-21G"
#         if ("rem", "rttddft") not in settings:
#             settings["rem", "rttddft"] = 1
#         self.tdscf_settings = tdscf_settings or mtr.Settings()

#     def compute(self):
#         tdscf_input_path = mtr.expand(os.path.join(self.work_directory, "TDSCF.prm"))
#         keys = tuple(str(next(iter(k))) for k in self.tdscf_settings)
#         max_length = max(len(k) for k in keys)
#         with open(mtr.expand(tdscf_input_path), "w") as f:
#             f.write(
#                 "\n".join(
#                     k + " " * (max_length - len(k) + 1) + str(self.tdscf_settings[k])
#                     for k in keys
#                 )
#             )
#         mtr.QChemInput(settings=settings).write(filepath=self.input_path)
#         try:
#             os.makedirs(mtr.expand(os.path.join(self.work_directory, "logs")))
#         except FileExistsError:
#             pass
#         os.environ["QCSCRATCH"] = self.scratch_directory
#         with open(self.output_path, "w") as f:
#             if self.parallel:
#                 subprocess.call(
#                     [self.executable, "-nt", str(self.num_cores), self.input_path],
#                     stdout=f,
#                     stderr=subprocess.STDOUT,
#                 )
#             else:
#                 subprocess.call([self.executable, self.input_path], stdout=f)

#         # FIXME: finish with output


class QChemSinglePoint(QChemBaseTask):
    def parse(self, output: str) -> Any:
        try:
            energy = cclib.io.ccread(output).scfenergies * mtr.eV
        except AttributeError:
            energy = None

        return energy

    def defaults(self, settings: mtr.Settings) -> mtr.Settings:
        if ("rem", "exchange") not in settings and (
            "rem",
            "method",
        ) not in settings:
            settings["rem", "exchange"] = "HF"
        if ("rem", "basis") not in settings:
            settings["rem", "basis"] = "3-21G"
        if ("rem", "jobtype") not in settings:
            settings["rem", "jobtype"] = "sp"

        return settings


class QChemSinglePointFrontier(QChemBaseTask):
    def parse(self, output: str) -> Any:
        try:
            out = cclib.io.ccread(output)
            energy = out.scfenergies * mtr.eV
            moenergies = out.moenergies
            homo_indices = out.homos

            homos = []
            lumos = []

            for moe, h in zip(moenergies, homo_indices):
                homo, lumo = moe[h : h + 2]
                homos.append(homo)
                lumos.append(lumo)

            homo = max(homos) * mtr.eV
            lumo = min(lumos) * mtr.eV
        except AttributeError:
            energy = None
            homo = None
            lumo = None

        return energy, homo, lumo

    def defaults(self, settings: mtr.Settings) -> mtr.Settings:
        if ("rem", "exchange") not in settings and ("rem", "method") not in settings:
            settings["rem", "exchange"] = "HF"
        if ("rem", "basis") not in settings:
            settings["rem", "basis"] = "3-21G"
        if ("rem", "jobtype") not in settings:
            settings["rem", "jobtype"] = "sp"

        return settings


def _nto_pairs(
    molecule: mtr.Molecule,
    settings: mtr.Settings,
    num_nto_pairs: int,
    n_x: int,
    n_y: int,
    n_z: int,
) -> mtr.QChemInput:
    # number of alpha electrons
    n_alpha = int(round((sum(molecule.atomic_numbers) + molecule.charge) / 2))

    inp = mtr.QChemInput(settings=mtr.Settings())

    for i in range(settings["rem", "cis_n_roots"]):
        s = copy.deepcopy(settings)
        s["rem", "scf_guess"] = "read"
        s["rem", "skip_cis_rpa"] = True
        s["rem", "nto_pairs"] = True
        s["rem", "make_cube_files"] = "ntos"
        s["rem", "cubefile_state"] = i + 1
        s["plots", "comment"] = (
            f"\n  {n_x} -10.0 10.0\n"
            + f"  {n_y} -10.0 10.0\n"
            + f"  {n_z} -10.0 10.0\n"
            + f"  {2*num_nto_pairs} 0 0 0\n  "
            + " ".join(
                f"{n_alpha + i}" for i in range(-num_nto_pairs + 1, num_nto_pairs + 1)
            )
        )

        inp += mtr.QChemInput(settings=s)

    return inp
