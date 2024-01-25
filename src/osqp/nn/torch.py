import numpy as np
import scipy.sparse as spa
import torch
from torch.nn import Module
from torch.autograd import Function

import osqp


def to_numpy(t):
    if t is None:
        return None
    elif t.nelement() == 0:
        return np.array([])
    else:
        return t.cpu().detach().numpy()


class OSQP(Module):
    def __init__(
        self,
        P_idx,
        P_shape,
        A_idx,
        A_shape,
        eps_rel=1e-5,
        eps_abs=1e-5,
        verbose=False,
        max_iter=10000,
        algebra='builtin',
        solver_type='direct',
    ):
        super().__init__()
        self.P_idx, self.P_shape = P_idx, P_shape
        self.A_idx, self.A_shape = A_idx, A_shape
        self.eps_rel, self.eps_abs = eps_rel, eps_abs
        self.verbose = verbose
        self.max_iter = max_iter
        self.algebra = algebra
        self.solver_type = solver_type

    def forward(self, P_val, q_val, A_val, l_val, u_val):
        return _OSQP_Fn(
            P_idx=self.P_idx,
            P_shape=self.P_shape,
            A_idx=self.A_idx,
            A_shape=self.A_shape,
            eps_rel=self.eps_rel,
            eps_abs=self.eps_abs,
            verbose=self.verbose,
            max_iter=self.max_iter,
            algebra=self.algebra,
            solver_type=self.solver_type,
        )(P_val, q_val, A_val, l_val, u_val)


def _OSQP_Fn(
    P_idx,
    P_shape,
    A_idx,
    A_shape,
    eps_rel,
    eps_abs,
    verbose,
    max_iter,
    algebra,
    solver_type,
):
    solvers = []

    m, n = A_shape  # Problem size

    class _OSQP_FnFn(Function):
        @staticmethod
        def forward(ctx, P_val, q_val, A_val, l_val, u_val):
            """Solve a batch of QPs using OSQP.

            This function solves a batch of QPs, each optimizing over
            `n` variables and having `m` constraints.

            The optimization problem for each instance in the batch
            (dropping indexing from the notation) is of the form

                \\hat x =   argmin_x 1/2 x' P x + q' x
                        subject to l <= Ax <= u

            where P \\in S^{n,n},
                S^{n,n} is the set of all positive semi-definite matrices,
                q \\in R^{n}
                A \\in R^{m,n}
                l \\in R^{m}
                u \\in R^{m}

            These parameters should all be passed to this function as
            Variable- or Parameter-wrapped Tensors.
            (See torch.autograd.Variable and torch.nn.parameter.Parameter)

            If you want to solve a batch of QPs where `n` and `m`
            are the same, but some of the contents differ across the
            minibatch, you can pass in tensors in the standard way
            where the first dimension indicates the batch example.
            This can be done with some or all of the coefficients.

            You do not need to add an extra dimension to coefficients
            that will not change across all of the minibatch examples.
            This function is able to infer such cases.

            If you don't want to use any constraints, you can set the
            appropriate values to:

                e = Variable(torch.Tensor())

            """

            def _get_update_flag(n_batch: int) -> bool:
                    """
                    This is a helper function that returns a flag if we need to update the solvers
                    or generate them. Raises an RuntimeError if the number of solvers is invalid.
                    """
                    num_solvers = len(solvers)
                    if num_solvers not in (0, n_batch):
                        raise RuntimeError(f"Invalid number of solvers: expected 0 or {n_batch},"
                                       f" but got {num_solvers}.")
                    return num_solvers==n_batch

            def _setup_update_solvers(n_batch: int, **kwargs) -> None:
                """
                This is a helper function that setups new solvers if solvers is empty or updates
                the list if it exists. Raises an RuntimeError if the number of solvers is invalid.
                """

                

                update_flag = _get_update_flag(solvers, n_batch)
                P_val, P_idx = kwargs.get("P_val"), kwargs.get("P_idx")
                A_val, A_idx = kwargs.get("A_val"), kwargs.get("A_idx")
                P_shape, A_shape = kwargs.get("P_shape"), kwargs.get("A_shape")
                q, l, u = kwargs.get("q"), kwargs.get("l"), kwargs.get("u")

                for i in range(n_batch):
                    # Solve QP
                    # TODO: Cache solver object in between
                    P = spa.csc_matrix((to_numpy(P_val[i]), P_idx), shape=P_shape)
                    A = spa.csc_matrix((to_numpy(A_val[i]), A_idx), shape=A_shape)
                    if update_flag:
                        solvers[i].update(q=q[i], l=l[i], u=u[i], Px=P, Px_idx=P_idx,
                                          Ax=A, Ax_idx=A_idx)
                    else: #setup
                        solver = osqp.OSQP(algebra=algebra) #TODO: When Ian introduces hard copy, generate only once
                        solver.setup(
                            P,
                            q[i],
                            A,
                            l[i],
                            u[i],
                            solver_type=solver_type,
                            verbose=verbose,
                            eps_abs=eps_abs,
                            eps_rel=eps_rel,
                        )
                        solvers.append(solver)
               

            params = [P_val, q_val, A_val, l_val, u_val]

            for p in params:
                assert p.ndimension() <= 2, 'Unexpected number of dimensions'

            batch_mode = np.any([t.ndimension() > 1 for t in params])
            if not batch_mode:
                n_batch = 1
            else:
                batch_sizes = [t.size(0) if t.ndimension() == 2 else 1 for t in params]
                n_batch = max(batch_sizes)

            dtype = P_val.dtype
            device = P_val.device

            # TODO (Bart): create CSC matrix during initialization. Then
            # just reassign the mat.data vector with A_val and P_val

            for i, p in enumerate(params):
                if p.ndimension() == 1:
                    params[i] = p.unsqueeze(0).expand(n_batch, p.size(0))

            [P_val, q_val, A_val, l_val, u_val] = params
            assert A_val.size(1) == len(A_idx[0]), 'Unexpected size of A'
            assert P_val.size(1) == len(P_idx[0]), 'Unexpected size of P'

            q = [to_numpy(q_val[i]) for i in range(n_batch)]
            l = [to_numpy(l_val[i]) for i in range(n_batch)]
            u = [to_numpy(u_val[i]) for i in range(n_batch)]

            # Perform forward step solving the QPs
            x_torch = torch.zeros((n_batch, n), dtype=dtype, device=device)

            x = []
            for i in range(n_batch):
                # Solve QP
                # TODO: Cache solver object in between
                update_flag = _get_update_flag(solvers, n_batch)
                P = spa.csc_matrix((to_numpy(P_val[i]), P_idx), shape=P_shape)
                A = spa.csc_matrix((to_numpy(A_val[i]), A_idx), shape=A_shape)
                if update_flag:
                        solver = solvers[i]
                        solver.update(q=q[i], l=l[i], u=u[i], Px=P, Px_idx=P_idx,
                                          Ax=A, Ax_idx=A_idx)
                else:
                    solver = osqp.OSQP(algebra=algebra) #TODO: Deep copy when available
                    solver.setup(
                        P,
                        q[i],
                        A,
                        l[i],
                        u[i],
                        solver_type=solver_type,
                        verbose=verbose,
                        eps_abs=eps_abs,
                        eps_rel=eps_rel,
                    )
                result = solver.solve()
                if update_flag:
                    solvers[i] = solver
                else:
                    solvers.append(solver)
                status = result.info.status
                if status != 'solved':
                    # TODO: We can replace this with something calmer and
                    # add some more options around potentially ignoring this.
                    raise RuntimeError(f'Unable to solve QP, status: {status}')
                x.append(result.x)

                # This is silently converting result.x to the same
                # dtype and device as x_torch.
                x_torch[i] = torch.from_numpy(result.x)

            # Return solutions
            if not batch_mode:
                x_torch = x_torch.squeeze(0)

            return x_torch

        @staticmethod
        def backward(ctx, dl_dx_val):
            dtype = dl_dx_val.dtype
            device = dl_dx_val.device

            batch_mode = dl_dx_val.ndimension() == 2

            if not batch_mode:
                dl_dx_val = dl_dx_val.unsqueeze(0)

            n_batch = dl_dx_val.size(0)
            dtype = dl_dx_val.dtype
            device = dl_dx_val.device

            # Convert dl_dx to numpy
            dl_dx = to_numpy(dl_dx_val)

            # Convert to torch tensors
            nnz_P = len(P_idx[0])
            nnz_A = len(A_idx[0])
            dP = torch.zeros((n_batch, nnz_P), dtype=dtype, device=device)
            dq = torch.zeros((n_batch, n), dtype=dtype, device=device)
            dA = torch.zeros((n_batch, nnz_A), dtype=dtype, device=device)
            dl = torch.zeros((n_batch, m), dtype=dtype, device=device)
            du = torch.zeros((n_batch, m), dtype=dtype, device=device)

            for i in range(n_batch):
                solvers[i].adjoint_derivative_compute(dx=dl_dx[i])
                dPi_np, dAi_np = solvers[i].adjoint_derivative_get_mat(as_dense=False, dP_as_triu=False)
                dqi_np, dli_np, dui_np = solvers[i].adjoint_derivative_get_vec()
                dq[i], dl[i], du[i] = [torch.from_numpy(d) for d in [dqi_np, dli_np, dui_np]]
                dP[i], dA[i] = [torch.from_numpy(d.x) for d in [dPi_np, dAi_np]]

            grads = [dP, dq, dA, dl, du]

            if not batch_mode:
                for i, g in enumerate(grads):
                    grads[i] = g.squeeze()

            return tuple(grads)

    return _OSQP_FnFn.apply
