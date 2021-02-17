from typing import Any, Union
from copy import deepcopy

import numpy as np
from scipy import interpolate as sci_interp
from casadi import Function, DM
from matplotlib import pyplot as plt

from ..limits.path_conditions import InitialGuess, InitialGuessList
from ..misc.enums import ControlType, CostType, Shooting, InterpolationType
from ..misc.utils import check_version


class Solution:
    """
    Data manipulation and storage

    Methods
    -------
    to_matrix(self, idx: Union[int, list, tuple] = (), phase_idx: Union[int, list, tuple] = (),
            node_idx: Union[int, list, tuple] = (), concatenate_phases: bool = True) -> np.ndarray
        Parse the data into a np.ndarray
    set_time_per_phase(self, new_t: list)
        Set the time vector of the phase
    get_time_per_phase(self, phases: Union[int, list, tuple] = (), merge_phases: bool = False) -> np.ndarray
        Get the time for each phase
    get_data(ocp: OptimalControlProgram, sol_x: dict, get_states: bool = True, get_controls: bool = True,
            get_parameters: bool = False, phase_idx: Union[int, list, tuple] = None, integrate: bool = False,
            interpolate_n_frames: int = -1, merge_phases: bool = True,) -> tuple
        Comprehensively parse the data from a solution
    get_data_object(ocp: OptimalControlProgram, V: np.ndarray, phase_idx: Union[int, list, tuple] = None,
            integrate: bool = False, interpolate_n_frames: int = -1, merge_phases: bool = True) -> tuple
        Parse an unstructured vector of data of data into their list of Phase format
    _get_data_integrated_from_V(ocp: OptimalControlProgram, data_states: dict,
            data_controls: dict, data_parameters: dict) -> dict
        Integrates the states
    _data_concatenated(data: dict) -> dict
        Concatenate all the phases
    _get_data_interpolated_from_V(data_states: dict, n_frames: int) -> dict
        Interpolate the states
    _horzcat_node(self, dt: float, x_to_add: np.ndarray, idx_phase: int, idx_node: int)
        Concatenate the nodes of a Phase into a np.ndarray
    _get_phase(V_phase: np.ndarray, var_size: int, n_nodes: int, offset: int, n_variables: int,
            duplicate_last_column: bool) -> np.ndarray
        Extract the data of a specific phase from an unstructured vector of data
    _vertcat(data: np.ndarray, keys: str, phases: Union[int, list, tuple] = (), nodes: Union[int, list, tuple] = ())
        Add new elements (rows) to the data
    _append_phase(self, time: np.ndarray, phase: "Data.Phase")
        Add a new phase to the phase list
    """

    class SimplifiedNLP:
        def __init__(self, nlp):
            self.model = nlp.model
            self.nx = nlp.nx
            self.nu = nlp.nu
            self.dynamics = nlp.dynamics
            self.n_integration_steps = nlp.n_integration_steps
            self.mapping = nlp.mapping
            self.var_states = nlp.var_states
            self.control_type = nlp.control_type
            self.J = nlp.J
            self.g = nlp.g
            self.ns = nlp.ns

    class SimplifiedOCP:
        def __init__(self, ocp):
            self.nlp = [Solution.SimplifiedNLP(nlp) for nlp in ocp.nlp]
            self.v = ocp.v
            self.J = ocp.J
            self.g = ocp.g
            self.phase_transitions = ocp.phase_transitions
            self.prepare_plots = ocp.prepare_plots

    def __init__(self, ocp, sol):
        self.ocp = Solution.SimplifiedOCP(ocp) if ocp else None
        self.ns = [nlp.ns for nlp in self.ocp.nlp]

        # Current internal state of the data
        self.is_interpolated = False
        self.is_integrated = False
        self.is_merged = False

        self.vector = None
        self.cost = None
        self.constraints = None

        self.lam_g = None
        self.lam_p = None
        self.lam_x = None
        self.time_to_optimize = None
        self.iterations = None

        # Extract the data now for further use
        self._states, self._controls, self._parameters = None, None, None
        self.phase_time = None

        def init_from_dict(sol: dict):
            self.vector = sol["x"] if isinstance(sol, dict) and "x" in sol else sol
            self.cost = sol["f"] if isinstance(sol, dict) and "f" in sol else None
            self.constraints = sol["g"] if isinstance(sol, dict) and "g" in sol else None

            self.lam_g = sol["lam_g"] if isinstance(sol, dict) and "lam_g" in sol else None
            self.lam_p = sol["lam_p"] if isinstance(sol, dict) and "lam_p" in sol else None
            self.lam_x = sol["lam_x"] if isinstance(sol, dict) and "lam_x" in sol else None
            self.time_to_optimize = sol["time_tot"] if isinstance(sol, dict) and "time_tot" in sol else None
            self.iterations = sol["iter"] if isinstance(sol, dict) and "iter" in sol else None

            # Extract the data now for further use
            self._states, self._controls, self._parameters = self.ocp.v.to_dictionaries(self.vector)
            self._complete_control()
            self.phase_time = self.ocp.v.extract_phase_time(self.vector)

        def init_from_initial_guess(sol: list):
            n_param = len(ocp.v.parameters_in_list)

            # Sanity checks
            for i in range(len(sol)):  # Convert to list if necessary and copy for as many phases there are
                if isinstance(sol[i], InitialGuess):
                    tp = InitialGuessList()
                    for _ in range(len(self.ns)):
                        tp.add(deepcopy(sol[i].init), interpolation=sol[i].init.type)
                    sol[i] = tp
            if sum([isinstance(s, InitialGuessList) for s in sol]) != 2:
                raise ValueError(
                    "solution must be a solution dict, "
                    "an InitialGuess[List] of len 2 or 3 (states, controls, parameters), "
                    "or a None"
                )
            if sum([len(s) != len(self.ns) if p != 3 else False for p, s in enumerate(sol)]) != 0:
                raise ValueError("The InitialGuessList len must match the number of phases")
            if n_param != 0:
                if len(sol) != 3 and len(sol[2]) != 1 and sol[2][0].shape != (n_param, 1):
                    raise ValueError(
                        "The 3rd element is the InitialGuess of the parameter and "
                        "should be a unique vector of size equal to n_param"
                    )

            self.vector = np.ndarray((0, 1))
            sol_states, sol_controls = sol[0], sol[1]
            for p, s in enumerate(sol_states):
                s.init.check_and_adjust_dimensions(self.ocp.nlp[p].nx, self.ocp.nlp[p].ns + 1, "states")
                for i in range(self.ns[p] + 1):
                    self.vector = np.concatenate((self.vector, s.init.evaluate_at(i)[:, np.newaxis]))
            for p, s in enumerate(sol_controls):
                control_type = self.ocp.nlp[p].control_type
                if control_type == ControlType.CONSTANT:
                    off = 0
                elif control_type == ControlType.LINEAR_CONTINUOUS:
                    off = 1
                else:
                    raise NotImplementedError(f"control_type {control_type} is not implemented in Solution")
                s.init.check_and_adjust_dimensions(self.ocp.nlp[p].nu, self.ns[p], "controls")
                for i in range(self.ns[p] + off):
                    self.vector = np.concatenate((self.vector, s.init.evaluate_at(i)[:, np.newaxis]))

            if n_param:
                sol_params = sol[2]
                for p, s in enumerate(sol_params):
                    self.vector = np.concatenate((self.vector, np.repeat(s.init, self.ns[p] + 1)[:, np.newaxis]))

            self._states, self._controls, self._parameters = self.ocp.v.to_dictionaries(self.vector)
            self._complete_control()
            self.phase_time = self.ocp.v.extract_phase_time(self.vector)

        def init_from_vector(sol):
            self.vector = sol
            self._states, self._controls, self._parameters = self.ocp.v.to_dictionaries(self.vector)
            self._complete_control()
            self.phase_time = self.ocp.v.extract_phase_time(self.vector)

        def init_from_none():
            self._states, self._controls, self._parameters = {}, {}, {}
            self.phase_time = []
            self.ns = []

        if isinstance(sol, dict):
            init_from_dict(sol)
        elif isinstance(sol, (list, tuple)) and len(sol) in (2, 3):
            init_from_initial_guess(sol)
        elif isinstance(sol, (np.ndarray, DM)):
            init_from_vector(sol)
        elif sol is None:
            init_from_none()
        else:
            raise ValueError("Solution called with unknown initializer")

    def copy(self, skip_data=False):
        new = Solution(self.ocp, None)

        new.vector = deepcopy(self.vector)
        new.cost = deepcopy(self.cost)
        new.constraints = deepcopy(self.constraints)

        new.lam_g = deepcopy(self.lam_g)
        new.lam_p = deepcopy(self.lam_p)
        new.lam_x = deepcopy(self.lam_x)
        new.time_to_optimize = deepcopy(self.time_to_optimize)
        new.iterations = deepcopy(self.iterations)

        new.is_interpolated = deepcopy(self.is_interpolated)
        new.is_integrated = deepcopy(self.is_integrated)
        new.is_merged = deepcopy(self.is_merged)

        new.phase_time = deepcopy(self.phase_time)
        new.ns = deepcopy(self.ns)

        if skip_data:
            new._states, new._controls, new._parameters = None, None, None
        else:
            new._states = deepcopy(self._states)
            new._controls = deepcopy(self._controls)
            new._parameters = deepcopy(self._parameters)

        return new

    @property
    def states(self):
        return self._states[0] if len(self._states) == 1 else self._states

    @property
    def controls(self):
        if not self._controls:
            raise RuntimeError(
                "There is no controls in the solution. "
                "This may happen in " "previously integrated and interpolated structure"
            )
        return self._controls[0] if len(self._controls) == 1 else self._controls

    @property
    def parameters(self):
        return self._parameters

    def integrate(
        self, shooting_type: Shooting = Shooting.MULTIPLE, merge_phases: bool = False, continuous: bool = True
    ):
        """
        Integrates the states

        Returns
        -------
        The dictionary of states integrated
        """

        # Sanity check
        if self.is_integrated:
            raise RuntimeError("Cannot integrate twice")
        if self.is_interpolated:
            raise RuntimeError("Cannot integrate after interpolating")
        if self.is_merged:
            raise RuntimeError("Cannot integrate after merging phases")

        # Copy the data
        out = self.copy(skip_data=True)

        ocp = out.ocp
        out._states = []
        for _ in range(len(self._states)):
            out._states.append({})

        params = self._parameters["all"]
        x0 = self._states[0]["all"][:, 0]
        for p in range(len(self._states)):
            shape = self._states[p]["all"].shape
            if continuous:
                n_steps = ocp.nlp[p].n_integration_steps
                out.ns[p] *= ocp.nlp[p].n_integration_steps
                out._states[p]["all"] = np.ndarray((shape[0], (shape[1] - 1) * n_steps + 1))
            else:
                n_steps = ocp.nlp[p].n_integration_steps + 1
                out.ns[p] *= ocp.nlp[p].n_integration_steps + 1
                out._states[p]["all"] = np.ndarray((shape[0], (shape[1] - 1) * n_steps))

            # Integrate
            if shooting_type == Shooting.SINGLE:
                if p != 0:
                    val = self.ocp.phase_transitions[p - 1].casadi_function(self.vector)
                    if val.shape[0] != x0.shape[0]:
                        raise RuntimeError(f"Phase transition must have the same number of states ({val.shape[0]}) "
                                           f"when integrating with Shooting.SINGLE. If it is not possible, "
                                           f"please integrate with Shooting.SINGLE_RESET_AT_PHASE")
                    x0 += val
            else:
                x0 = self._states[p]["all"][:, 0]
            for n in range(self.ns[p]):
                if self.ocp.nlp[p].control_type == ControlType.CONSTANT:
                    u = self._controls[p]["all"][:, n]
                elif self.ocp.nlp[p].control_type == ControlType.LINEAR_CONTINUOUS:
                    u = self._controls[p]["all"][:, n:n+2]
                else:
                    raise NotImplementedError(f"ControlType {self.ocp.nlp[p].control_type} "
                                              f"not yet implemented in integrating")
                integrated = np.array(ocp.nlp[p].dynamics[n](x0=x0, p=u, params=params)["xall"])
                cols = (
                    range(n * n_steps, (n + 1) * n_steps + 1) if continuous else range(n * n_steps, (n + 1) * n_steps)
                )
                out._states[p]["all"][:, cols] = integrated
                x0 = self._states[p]["all"][:, n + 1] if shooting_type == Shooting.MULTIPLE else integrated[:, -1]

            # Dispatch the integrated values to all the keys
            off = 0
            for key in ocp.nlp[p].var_states:
                out._states[p][key] = out._states[p]["all"][off : off + ocp.nlp[p].var_states[key], :]
                off += ocp.nlp[p].var_states[key]

        if merge_phases:
            out._states, _, out.phase_time, out.ns = out._merge_phases(skip_controls=True, force_skip_last_copy=not continuous)
            out.is_merged = True

        out.is_integrated = True
        return out

    def interpolate(self, n_frames: Union[int, list, tuple]):
        """
        Interpolate the states

        Parameters
        ----------
        n_frames: int
            The number of frames to interpolate the data

        Returns
        -------
        The dictionary of states interpolated
        """

        out = self.copy(skip_data=True)
        if isinstance(n_frames, int):
            # Todo interpolate relative to time of the phase and not relative to number of frames in the phase
            data_states, _, out.phase_time, out.ns = self._merge_phases(skip_controls=True)
            n_frames = [n_frames]
            out.is_merged = True
        elif isinstance(n_frames, (list, tuple)) and len(n_frames) == len(self._states):
            data_states = self._states
        else:
            raise ValueError(
                "n_frames should either be a int to merge_phases phases "
                "or a list of int of the number of phases dimension"
            )

        out._states = []
        for _ in range(len(data_states)):
            out._states.append({})
        for p in range(len(data_states)):
            x_phase = data_states[p]["all"]
            n_elements = x_phase.shape[0]

            t_phase = np.linspace(out.phase_time[p], out.phase_time[p] + out.phase_time[p + 1], x_phase.shape[1])
            t_int = np.linspace(t_phase[0], t_phase[-1], n_frames[p])

            x_interpolate = np.ndarray((n_elements, n_frames[p]))
            for j in range(n_elements):
                s = sci_interp.splrep(t_phase, x_phase[j, :])
                x_interpolate[j, :] = sci_interp.splev(t_int, s)
            out._states[p]["all"] = x_interpolate

            offset = 0
            for key in data_states[p]:
                if key == "all":
                    continue
                n_elements = data_states[p][key].shape[0]
                out._states[p][key] = out._states[p]["all"][offset : offset + n_elements]
                offset += n_elements

        out.is_interpolated = True
        return out

    def merge_phases(self):
        new = self.copy(skip_data=True)
        new._parameters = deepcopy(self._parameters)
        new._states, new._controls, new.phase_time, new.ns = self._merge_phases()
        new.is_merged = True
        return new

    def _merge_phases(self, skip_states=False, skip_controls=False, force_skip_last_copy=False) -> tuple:
        """
        Concatenate all the phases

        Parameters
        ----------

        Returns
        -------
        The new dictionary of data with the phases merged
        """

        if self.is_merged or len(self._states) == 1:
            return deepcopy(self._states), deepcopy(self._controls), deepcopy(self.phase_time), deepcopy(self.ns)

        def _merge(data):
            if isinstance(data, dict):
                return data

            # Sanity check (all phases must contain the same keys with the same dimensions)
            keys = data[0].keys()
            sizes = [data[0][d].shape[0] for d in data[0]]
            for d in data:
                if d.keys() != keys or [d[key].shape[0] for key in d] != sizes:
                    raise RuntimeError("Program dimension must be coherent across phases to merge_phases them")

            data_out = [{}]
            for i, key in enumerate(keys):
                data_out[0][key] = np.ndarray((sizes[i], 0))

            for p in range(len(data)):
                d = data[p]
                for key in d:
                    data_out[0][key] = np.concatenate((data_out[0][key], d[key][:, : self.ns[p]]), axis=1)
            if not force_skip_last_copy:
                for key in data[-1]:
                    data_out[0][key] = np.concatenate((data_out[0][key], data[-1][key][:, -1][:, np.newaxis]), axis=1)

            return data_out

        out_states = _merge(self.states) if not skip_states and self._states else None
        out_controls = _merge(self.controls) if not skip_controls and self._controls else None
        phase_time = [0] + [sum([self.phase_time[i + 1] for i in range(len(self.phase_time) - 1)])]
        ns = [sum(self.ns)]

        return out_states, out_controls, phase_time, ns

    def _complete_control(self):
        for p, nlp in enumerate(self.ocp.nlp):
            if nlp.control_type == ControlType.CONSTANT:
                for key in self._controls[p]:
                    self._controls[p][key] = np.concatenate(
                        (self._controls[p][key], self._controls[p][key][:, -1][:, np.newaxis]), axis=1
                    )
            elif nlp.control_type == ControlType.LINEAR_CONTINUOUS:
                pass
            else:
                raise NotImplementedError(f"ControlType {nlp.control_type} is not implemented  in _complete_control")

    def graphs(
        self,
        automatically_organize: bool = True,
        adapt_graph_size_to_bounds: bool = False,
        show_now: bool = True,
        shooting_type=Shooting.MULTIPLE,
    ):
        """
        Prepare the graphs of the simulation

        Parameters
        ----------
        automatically_organize: bool
            If the figures should be spread on the screen automatically
        adapt_graph_size_to_bounds: bool
            If the plot should adapt to bounds (True) or to data (False)
        show_now: bool
            If the show method should be called. This is blocking

        Returns
        -------

        """

        if self.is_merged or self.is_interpolated or self.is_integrated:
            raise NotImplementedError("It is not possible to graph a modified Solution yet")

        plot_ocp = self.ocp.prepare_plots(automatically_organize, adapt_graph_size_to_bounds, shooting_type)
        plot_ocp.update_data(self.vector)
        if show_now:
            plt.show()

    def animate(self, n_frames: int = 0, show_now: bool = True, **kwargs: Any) -> list:
        """
        An interface to animate solution with bioviz

        Parameters
        ----------
        n_frames: int
            The number of frames to interpolate to
        show_now: bool
            If the bioviz exec() function should be called. This is blocking
        kwargs: dict
            Any parameters to pass to bioviz

        Returns
        -------
            A list of bioviz structures (one for each phase)
        """

        try:
            import bioviz
        except ModuleNotFoundError:
            raise RuntimeError("bioviz must be install to animate the model")
        check_version(bioviz, "2.0.1", "2.1.0")

        if n_frames == 0:
            try:
                states_to_animate = self.merge_phases().states
            except RuntimeError:
                states_to_animate = self.states
        elif n_frames == -1:
            states_to_animate = self.states
        else:
            states_to_animate = self.interpolate(n_frames).states

        if not isinstance(states_to_animate, (list, tuple)):
            states_to_animate = [states_to_animate]

        all_bioviz = []
        for idx_phase, data in enumerate(states_to_animate):
            all_bioviz.append(bioviz.Viz(loaded_model=self.ocp.nlp[idx_phase].model, **kwargs))
            all_bioviz[-1].load_movement(self.ocp.nlp[idx_phase].mapping["q"].to_second.map(data["q"]))

        if show_now:
            b_is_visible = [True] * len(all_bioviz)
            while sum(b_is_visible):
                for i, b in enumerate(all_bioviz):
                    if b.vtk_window.is_active:
                        b.update()
                    else:
                        b_is_visible[i] = False
        else:
            return all_bioviz

    def print(self, data_type: CostType = CostType.ALL):
        def print_objective_functions(ocp, sol):
            """
            Print the values of each objective function to the console
            """

            def __extract_objective(pen: dict):
                """
                Extract objective function from a penalty

                Parameters
                ----------
                pen: dict
                    The penalty to extract the value from

                Returns
                -------
                The value extract
                """

                # TODO: This should be done in bounds and objective functions, so it is available for all the code
                val_tp = Function("val_tp", [ocp.v.vector], [pen["val"]]).expand()(sol.vector)
                if pen["target"] is not None:
                    # TODO Target should be available to constraint?
                    nan_idx = np.isnan(pen["target"])
                    pen["target"][nan_idx] = 0
                    val_tp -= pen["target"]
                    if np.any(nan_idx):
                        val_tp[np.where(nan_idx)] = 0

                if pen["objective"].quadratic:
                    val_tp *= val_tp

                val = np.sum(val_tp)

                dt = Function("dt", [ocp.v.vector], [pen["dt"]]).expand()(sol.vector)
                val_weighted = pen["objective"].weight * val * dt
                return val, val_weighted

            print(f"\n---- COST FUNCTION VALUES ----")
            has_global = False
            running_total = 0
            for J in ocp.J:
                has_global = True
                val = []
                val_weighted = []
                for j in J:
                    out = __extract_objective(j)
                    val.append(out[0])
                    val_weighted.append(out[1])
                sum_val_weighted = sum(val_weighted)
                print(f"{J[0]['objective'].name}: {sum(val)} (weighted {sum_val_weighted})")
                running_total += sum_val_weighted
            if has_global:
                print("")

            for idx_phase, nlp in enumerate(ocp.nlp):
                print(f"PHASE {idx_phase}")
                for J in nlp.J:
                    val = []
                    val_weighted = []
                    for j in J:
                        out = __extract_objective(j)
                        val.append(out[0])
                        val_weighted.append(out[1])
                    sum_val_weighted = sum(val_weighted)
                    print(f"{J[0]['objective'].name}: {sum(val)} (weighted {sum_val_weighted})")
                    running_total += sum_val_weighted
                print("")
            print(f"Sum cost functions: {running_total}")
            print(f"------------------------------")

        def print_constraints(ocp, sol):
            """
            Print the values of each constraints with its lagrange multiplier to the console
            """

            if sol.constraints is None:
                return

            # Todo, min/mean/max
            print(f"\n--------- CONSTRAINTS ---------")
            idx = 0
            has_global = False
            for G in ocp.g:
                has_global = True
                g, next_idx = None, None
                for g in G:
                    next_idx = idx + g["val"].shape[0]
                if g:
                    print(f"{g['constraint'].name}: {np.sum(sol.constraints[idx:next_idx])}")
                idx = next_idx
            if has_global:
                print("")

            for idx_phase, nlp in enumerate(ocp.nlp):
                print(f"PHASE {idx_phase}")
                for G in nlp.g:
                    g, next_idx = None, idx
                    for g in G:
                        next_idx += g["val"].shape[0]
                    if g:
                        print(f"{g['constraint'].name}: {np.sum(sol.constraints[idx:next_idx])}")
                    idx = next_idx
                print("")
            print(f"------------------------------")

        if data_type == CostType.OBJECTIVES:
            print_objective_functions(self.ocp, self)
        elif data_type == CostType.CONSTRAINTS:
            print_constraints(self.ocp, self)
        elif data_type == CostType.ALL:
            self.print(CostType.OBJECTIVES)
            self.print(CostType.CONSTRAINTS)
        else:
            raise ValueError("print can only be called with CostType.OBJECTIVES or CostType.CONSTRAINTS")
