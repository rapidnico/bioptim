import numpy as np
import biorbd_casadi as biorbd
from bioptim import (
    OptimalControlProgram,
    DynamicsList,
    Dynamics,
    DynamicsFcn,
    ObjectiveList,
    ObjectiveFcn,
    ConstraintList,
    ConstraintFcn,
    BoundsList,
    QAndQDotBounds,
    InitialGuessList,
    OdeSolver,
    Node,
    Solver,
    Shooting,
    Solution,
    InitialGuess,
    CostType,
    InterpolationType,
)


def prepare_single_shooting(
    biorbd_model_path: str,
    n_shooting: int,
    final_time: float,
    ode_solver: OdeSolver,
) -> OptimalControlProgram:
    """
    Prepare the ss

    Returns
    -------
    The OptimalControlProgram ready to be solved
    """

    biorbd_model = biorbd.Model(biorbd_model_path)

    # Dynamics
    dynamics = Dynamics(DynamicsFcn.TORQUE_DRIVEN)

    # Initial guess
    x_init = InitialGuess([0] * (biorbd_model.nbQ() + biorbd_model.nbQdot()))

    # Problem parameters
    tau_min, tau_max, tau_init = -100, 100, 0

    u_init = InitialGuess([tau_init] * biorbd_model.nbGeneralizedTorque())

    return OptimalControlProgram(
        biorbd_model,
        dynamics,
        n_shooting,
        final_time,
        x_init,
        u_init,
        ode_solver=ode_solver,
        use_sx=False,
    )


def prepare_ocp(
    biorbd_model_path: str,
    n_shooting: int,
    final_time: float,
    ode_solver: OdeSolver,
    X0: np.array,
    slack: float,
) -> OptimalControlProgram:
    """
    Prepare the ocp


    Returns
    -------
    The OptimalControlProgram ready to be solved
    """
    biorbd_model = biorbd.Model(biorbd_model_path)

    # Problem parameters

    tau_min, tau_max, tau_init = -100, 100, 0

    # Add objective functions
    objective_functions = ObjectiveList()
    objective_functions.add(ObjectiveFcn.Lagrange.MINIMIZE_CONTROL, key="tau", weight=1)
    objective_functions.add(
        ObjectiveFcn.Mayer.SUPERIMPOSE_MARKERS,
        node=Node.START,
        first_marker="marker_point",
        second_marker="start",
        weight=10,
        axes=2,
    )
    objective_functions.add(
        ObjectiveFcn.Mayer.SUPERIMPOSE_MARKERS,
        node=Node.END,
        first_marker="marker_point",
        second_marker="end",
        weight=10,
    )

    # Dynamics
    dynamics = DynamicsList()
    dynamics.add(DynamicsFcn.TORQUE_DRIVEN)

    # Constraints
    constraints = ConstraintList()
    constraints.add(
        ConstraintFcn.SUPERIMPOSE_MARKERS, node=Node.START, first_marker="marker_point", second_marker="start"
    )
    constraints.add(ConstraintFcn.SUPERIMPOSE_MARKERS, node=Node.END, first_marker="marker_point", second_marker="end")

    # Path constraint
    x_bounds = BoundsList()
    x_bounds.add(bounds=QAndQDotBounds(biorbd_model))
    nQ = biorbd_model.nbQ()
    x_bounds[0].min[:nQ, 0] = X0[:nQ] - slack
    x_bounds[0].max[:nQ, 0] = X0[:nQ] + slack
    x_bounds[0].min[nQ:, 0] = -slack
    x_bounds[0].max[nQ:, 0] = +slack

    # Initial guess
    x_init = InitialGuessList()
    x_init.add(initial_guess=X0, interpolation=InterpolationType.CONSTANT)

    # Define control path constraint
    u_bounds = BoundsList()
    u_bounds.add([tau_min] * biorbd_model.nbGeneralizedTorque(), [tau_max] * biorbd_model.nbGeneralizedTorque())

    u_init = InitialGuessList()
    u_init.add([tau_init] * biorbd_model.nbGeneralizedTorque())

    return OptimalControlProgram(
        biorbd_model,
        dynamics,
        n_shooting,
        final_time,
        x_init,
        u_init,
        x_bounds,
        u_bounds,
        objective_functions,
        constraints,
        ode_solver=ode_solver,
        use_sx=False,
        n_threads=8,
    )


def main():
    """
    Defines a multiphase ocp and animate the results
    """
    model = "models/soft_contact_sphere.bioMod"
    ode_solver = OdeSolver.RK8()
    tf = 1
    ns = 100
    ocp = prepare_single_shooting(model, ns, tf, ode_solver)

    # Find equilibrium
    X = InitialGuess([0, 0.10, 0, 1e-10, 1e-10, 1e-10])
    U = InitialGuess([0, 0, 0])

    sol_from_initial_guess = Solution(ocp, [X, U])
    s = sol_from_initial_guess.integrate(shooting_type=Shooting.SINGLE, continuous=True)
    # s.animate()

    # Rolling Sphere at equilibrium
    x0 = s.states["q"][:, -1]
    dx0 = [0] * 3
    X0 = np.concatenate([x0, np.array(dx0)])
    X = InitialGuess(X0)
    U = InitialGuess([0, 0, -10])

    sol_from_initial_guess = Solution(ocp, [X, U])
    s = sol_from_initial_guess.integrate(shooting_type=Shooting.SINGLE, continuous=True)
    # s.animate()

    # Prepare OCP to reach the second marker
    ocp = prepare_ocp(model, 37, 0.37, OdeSolver.RK8(), X0, slack=1e-4)
    ocp.add_plot_penalty(CostType.ALL)
    ocp.print(to_graph=True)

    # --- Solve the program --- #
    solv = Solver.IPOPT(show_online_optim=False, show_options=dict(show_bounds=True))
    solv.set_linear_solver("mumps")
    solv.set_maximum_iterations(500)
    sol = ocp.solve(solv)

    sol.animate()
    sol.print()
    sol.graphs()


if __name__ == "__main__":
    main()
