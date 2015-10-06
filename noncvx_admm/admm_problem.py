"""
Copyright 2013 Steven Diamond

This file is part of CVXPY.

CVXPY is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

CVXPY is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with CVXPY.  If not, see <http://www.gnu.org/licenses/>.
"""

from __future__ import division
from noncvx_variable import NonCvxVariable
from boolean import Boolean
import multiprocessing
import cvxpy as cvx
import numpy as np

# Use ADMM to attempt non-convex problem.
def admm_basic(self, rho=0.5, iterations=5, random=False, *args, **kwargs):
    noncvx_vars = []
    for var in self.variables():
        if getattr(var, "noncvx", False):
            noncvx_vars += [var]
            var.init_z(random=False)
    # Form ADMM problem.
    obj = self.objective.args[0]
    for var in noncvx_vars:
        obj += (rho/2)*cvx.sum_entries(cvx.square(var - var.z + var.u))
    prob = cvx.Problem(cvx.Minimize(obj), self.constraints)
    # ADMM loop
    for i in range(iterations):
        result = prob.solve(*args, **kwargs)
        print "relaxation", result
        for idx, var in enumerate(noncvx_vars):
            var.z.value = var.project(var.value + var.u.value)
            # print idx, var.z.value, var.value, var.u.value
            var.u.value += var.value - var.z.value
    return polish(self, *args, **kwargs)

def admm_inner_iter(data):
    orig_prob, rho_val, gamma, max_iter, random, polish_best, args, kwargs = data
    noncvx_vars = get_noncvx_vars(orig_prob)

    # Form ADMM problem.
    obj = orig_prob.objective.args[0]
    for var in noncvx_vars:
        obj += (rho_val/2)*cvx.sum_squares(var - var.z + var.u)
    prob = cvx.Problem(cvx.Minimize(obj), orig_prob.constraints)

    best_so_far = [np.inf, {}]
    for var in noncvx_vars:
        var.init_z(random=random)
        var.init_u()
    # ADMM loop
    for k in range(max_iter):
        try:
            prob.get_problem_data(cvx.SCS)
            prob.solve(*args, **kwargs)
        except cvx.SolverError, e:
            pass
        if prob.status in [cvx.OPTIMAL, cvx.OPTIMAL_INACCURATE]:
            # print rho_val, k, best_so_far[0]
            for var in noncvx_vars:
                # if isinstance(var, Boolean):
                #     var.z.value = np.random.uniform(size=var.size) < var.value + var.u.value
                # else:
                var.z.value = var.project(var.value + var.u.value)
                # var.z.value = var.project(var.value + var.u.value + \
                #     np.random.normal(scale=sigma, size=var.size))
                var.u.value += var.value - var.z.value
                # if k == 0:
                #     print outer_iter
                #     print var.value
                #     print var.z.value
            old_vars = {var.id:var.value for var in orig_prob.variables()}
            if polish_best:
                # Try to polish.
                polish_opt_val, status = polish(orig_prob, *args, **kwargs)
            # print "polish_opt_val", polish_opt_val
            if not polish_best or status == cvx.INFEASIBLE:
                # Undo change in var.value.
                for var in orig_prob.variables():
                    if isinstance(var, NonCvxVariable):
                        var.value = var.z.value
                    else:
                        var.value = old_vars[var.id]

            merit = orig_prob.objective.value
            for constr in orig_prob.constraints:
                merit += gamma*cvx.sum_entries(constr.violation).value
            # print "objective", outer_iter, k, merit
            if merit <= best_so_far[0]:
                best_so_far[0] = merit
                best_so_far[1] = {v.id:v.value for v in prob.variables()}

            # # Restore variable values.
            # for var in noncvx_vars:
            #     var.value = var.z.value

        else:
            print prob.status
            break

    return best_so_far

# Use ADMM to attempt non-convex problem.
def admm(self, rho=None, max_iter=5, restarts=1,
         random=False, sigma=0.01, gamma=1e6, polish_best=True,
         num_procs=None,
         *args, **kwargs):
    # rho is a list of values, one for each restart.
    if rho is None:
        rho = [np.random.uniform() for i in range(restarts)]
    else:
        assert len(rho) == restarts
    # num_procs is the number of processors to launch.
    if num_procs is None:
        num_procs = multiprocessing.cpu_count()

    # Solve the relaxation.
    rel_val = self.solve(*args, **kwargs)
    print "lower bound", rel_val

    # Algorithm.
    pool = multiprocessing.Pool(num_procs)
    tmp_prob = cvx.Problem(self.objective, self.constraints)
    best_per_rho = pool.map(admm_inner_iter,
        [(tmp_prob, rho_val, gamma, max_iter, random, polish_best, args, kwargs) for rho_val in rho])
    # Merge best so far.
    argmin = min([(val[0], idx) for idx, val in enumerate(best_per_rho)])[1]
    best_so_far = best_per_rho[argmin]
    print "best found", best_so_far[0]
    # Unpack result.
    for var in self.variables():
        var.value = best_so_far[1][var.id]

    self._value = self.objective.value
    return self._value

def admm_consensus(self, rho=None, max_iter=5, restarts=1,
                   random=False, eps=1e-4, rel_eps=1e-4,
                   tau=1, tau_max=100, polish_best=True,
                   eps_abs=1e-3, eps_rel=1e-3,
                   *args, **kwargs):
    # rho is a list of values, one for each restart.
    if rho is None:
        rho = [np.random.uniform() for i in range(restarts)]
    else:
        assert len(rho) == restarts
    # Setup the problem.
    noncvx_vars = []
    for var in self.variables():
        if isinstance(var, NonCvxVariable):
            var.zbar = cvx.Parameter(*var.size)
            var.zbar.value = np.zeros(var.size)
            var.old_zbar = cvx.Parameter(*var.size)
            var.old_zbar.value = var.zbar.value
            var.old_z = cvx.Parameter(*var.size)
            var.old_x = cvx.Parameter(*var.size)
            var.u_nc = cvx.Parameter(*var.size)
            var.u_nc.value = np.zeros(var.size)
            noncvx_vars += [var]
    # Form ADMM problem.
    rho_param = cvx.Parameter(sign="positive")
    obj = self.objective.args[0]
    for var in noncvx_vars:
        obj += (rho_param/2)*cvx.sum_squares(var - var.zbar + var.u)
    prob = cvx.Problem(cvx.Minimize(obj), self.constraints)
    # Algorithm.
    best_so_far = [np.inf, np.inf, {}]
    for restart_iter, rho_val in enumerate(rho):
        for var in noncvx_vars:
            var.init_z(random=random)
        # ADMM loop
        for k in range(max_iter):
            rho_param.value = min(rho_val*tau**k, tau_max)
            # Record old values.
            for var in noncvx_vars:
                if var.value is None:
                    var.old_x.value = np.zeros(var.size)
                else:
                    var.old_x.value = var.value
                var.old_z.value = var.z.value

            try:
                prob.solve(*args, **kwargs)
            except cvx.SolverError, e:
                pass
            if prob.status in [cvx.OPTIMAL, cvx.OPTIMAL_INACCURATE]:
                for var in noncvx_vars:
                    var.z.value = var.project(var.zbar.value - var.u_nc.value)
                    # var.old_zbar.value = var.zbar.value
                    # Note var.u + var.u_nc = 0.
                    var.zbar.value = 0.5*(var.value + var.z.value)
                    var.u.value += var.value - var.zbar.value
                    var.u_nc.value += var.z.value - var.zbar.value

                opt_val = self.objective.value
                print "restart", restart_iter
                print "iter ", k
                # print "rho", rho_param.value
                print "original obj val", opt_val
                # print "augmented value", prob.value
                noncvx_inf = total_dist(noncvx_vars)
                print "noncvx_inf", noncvx_inf

                # Is the infeasibility better than best_so_far?
                error = get_error(noncvx_vars, eps, rel_eps)
                if is_better(noncvx_inf, opt_val, best_so_far, error):
                    best_so_far[0] = noncvx_inf
                    best_so_far[1] = opt_val
                    best_so_far[2] = {v.id:v.value for v in prob.variables()}
            else:
                print "fail"
                break

            # Convergence criteria.
            # # ||x^{k+1} - x^k||_2 + 2*||z^{k+1} - z^k||_2 <= eps_abs
            # x_error = np.sqrt(sum([cvx.sum_squares(v - v.old_x).value for v in noncvx_vars]))
            # z_error = np.sqrt(sum([cvx.sum_squares(v.z - v.old_z).value for v in noncvx_vars]))
            # print "x error = %f, z error = %f,  eps = %f" % (x_error, z_error, eps_abs)
            # if x_error + z_error <= eps_abs:
            #     break

    if polish_best:
        # Polish the best iterate.
        for var in prob.variables():
            var.value = best_so_far[2][var.id]
        opt_val, status = polish(self, *args, **kwargs)
        if status is cvx.OPTIMAL:
           error = get_error(noncvx_vars, eps, rel_eps)
           if is_better(0, opt_val, best_so_far, error):
                best_so_far[0] = 0
                best_so_far[1] = opt_val
                best_so_far[2] = {v.id:v.value for v in prob.variables()}

    # Unpack result.
    for var in prob.variables():
        var.value = best_so_far[2][var.id]
    error = get_error(noncvx_vars, eps, rel_eps)
    if best_so_far[0] < error:
        return best_so_far[1]
    else:
        return np.inf

def has_converged(noncvx_vars, eps_abs, eps_rel):
    # r^{k+1} = Ax^{k+1} - Bz^{k+1}
    # s^{k+1} = rho*A^T B(z^{k+1} - z^k)
    Ax_list = []
    Bz_list = []
    Bz_old_list = []
    Au_list = []
    for var in noncvx_vars:
        Ax_list += [cvx.vec(var), cvx.vec(var.z)]
        Bz_list += 2*[cvx.vec(var.zbar)]
        Bz_old_list += 2*[cvx.vec(var.old_zbar)]
        Au_list += [cvx.vec(var.u), cvx.vec(var.u_nc)]
    Ax_vec = cvx.vstack(*Ax_list).value
    Bz_vec = cvx.vstack(*Bz_list).value
    Bz_old_vec = cvx.vstack(*Bz_old_list).value
    Au_vec = cvx.vstack(*Au_list).value

    r_vec = Ax_vec - Bz_vec
    s_vec = rho_param*(Bz_vec - Bz_old_vec)
    p = n = Ax_vec.shape[0]

    # eps_pri = sqrt(p)*eps_abs +
    #           eps_rel*max(||Ax^{k+1}||_2, ||Bz^{k+1}||_2)
    # eps_dual = sqrt(n)*eps_abs + eps_rel*rho*||A^Tu^{k+1}||_2
    eps_pri = cvx.sqrt(p)*eps_abs + \
              eps_rel*cvx.max_elemwise(cvx.norm(Ax_vec, 2),
                                       cvx.norm(Bz_vec, 2))
    eps_dual = cvx.sqrt(n)*eps_abs + eps_rel*rho_param*cvx.norm(Au_vec, 2)
    print "||r||_2 = %f, ||s||_2 = %f, eps_pri = %f, eps_dual = %f" % \
    (cvx.norm(r_vec, 2).value, cvx.norm(s_vec, 2).value,
     eps_pri.value, eps_dual.value)
    return cvx.norm(r_vec, 2).value <= eps_pri.value and \
           cvx.norm(s_vec, 2).value <= eps_dual.value

def total_dist(noncvx_vars):
    """Get the total distance from the noncvx_var values
    to the nonconvex sets.
    """
    total = 0
    for var in noncvx_vars:
        total += var.dist(var.value)
    return total

def get_error(noncvx_vars, eps, rel_eps):
    """The error bound for comparing infeasibility.
    """
    error = sum([cvx.norm(cvx.vec(var)) for var in noncvx_vars]).value
    return eps + rel_eps*error

def is_better(noncvx_inf, opt_val, best_so_far, error):
    """Is the current result better than best_so_far?
    """
    inf_diff = best_so_far[0] - noncvx_inf
    return (inf_diff > error) or \
           (abs(inf_diff) <= error and opt_val < best_so_far[1])

def relax_and_round(self, *args, **kwargs):
    """Solve the relaxation, then project and polish.
    """
    self.solve(*args, **kwargs)
    for var in get_noncvx_vars(self):
        var.z.value = var.project(var.value)
    return polish(self, *args, **kwargs)

def repeated_rr(self, tau_init=1, tau_max=250, delta=1.1, max_iter=10,
                random=False, abs_eps=1e-4, rel_eps=1e-4, *args, **kwargs):
    noncvx_vars = get_noncvx_vars(self)
    # Form ADMM problem.
    tau_param = cvx.Parameter(sign="positive")
    tau_param.value = 0
    obj = self.objective.args[0]
    for var in noncvx_vars:
        obj = obj + (tau_param)*cvx.norm(var - var.z, 1)
    prob = cvx.Problem(cvx.Minimize(obj), self.constraints)
    # Algorithm.
    best_so_far = [np.inf, np.inf, {}]
    for var in noncvx_vars:
        var.init_z(random=random)
    # ADMM loop
    for k in range(max_iter):
        try:
            prob.solve(*args, **kwargs)
        except cvx.SolverError, e:
            pass
        if prob.status is cvx.OPTIMAL:
            opt_val = self.objective.value
            noncvx_inf = total_dist(noncvx_vars)
            print "iter ", k
            print "tau", tau_param.value
            print "original obj val", opt_val
            print "augmented value", prob.value
            print "noncvx_inf", noncvx_inf

            # Is the infeasibility better than best_so_far?
            error = get_error(noncvx_vars, abs_eps, rel_eps)
            if is_better(noncvx_inf, opt_val, best_so_far, error):
                best_so_far[0] = noncvx_inf
                best_so_far[1] = opt_val
                best_so_far[2] = {v.id:v.value for v in prob.variables()}

            for var in noncvx_vars:
                var.z.value = var.project(var.value)
        else:
            break

        # Update tau.
        tau_param.value = max(tau_init, min(tau_param.value*(delta**k), tau_max))

        # Convergence criteria.
        # TODO

        # # Polish the best iterate.
        # for var in prob.variables():
        #     var.value = best_so_far[2][var.id]
        # opt_val, status = polish(self, *args, **kwargs)
        # if status is cvx.OPTIMAL:
        #    error = get_error(noncvx_vars, eps, rel_eps)
        #    if is_better(0, opt_val, best_so_far, error):
        #         best_so_far[0] = 0
        #         best_so_far[1] = opt_val
        #         best_so_far[2] = {v.id:v.value for v in prob.variables()}

    # Unpack result.
    for var in prob.variables():
        var.value = best_so_far[2][var.id]
    error = get_error(noncvx_vars, abs_eps, rel_eps)
    if best_so_far[0] < error:
        return best_so_far[1]
    else:
        return np.inf

# Use ADMM to attempt non-convex problem.
def admm2(self, rho=0.5, iterations=5, random=False, *args, **kwargs):
    noncvx_vars = []
    for var in self.variables():
        if getattr(var, "noncvx", False):
            var.init_z(random=random)
            noncvx_vars += [var]
    # Form ADMM problem.
    obj = self.objective.args[0]
    for var in noncvx_vars:
        obj = obj + (rho/2)*cvx.sum_squares(var - var.z + var.u)
    prob = cvx.Problem(cvx.Minimize(obj), self.constraints)
    # ADMM loop
    best_so_far = np.inf
    for i in range(iterations):
        result = prob.solve(*args, **kwargs)
        for var in noncvx_vars:
            var.z.value = var.project(var.value + var.u.value)
            var.u.value += var.value - var.z.value
        polished_opt = polish(self, noncvx_vars, *args, **kwargs)
        if polished_opt < best_so_far:
            best_so_far = polished_opt
            print best_so_far
    return best_so_far

def get_noncvx_vars(prob):
    return [var for var in prob.variables() if getattr(var, "noncvx", False)]


def polish(prob, *args, **kwargs):
    # Fix noncvx variables and solve.
    fix_constr = []
    for var in get_noncvx_vars(prob):
        fix_constr += var.fix(var.z.value)
    prob = cvx.Problem(prob.objective, prob.constraints + fix_constr)
    prob.solve(*args, **kwargs)
    return prob.value, prob.status

# Add admm method to cvx Problem.
cvx.Problem.register_solve("admm_basic", admm_basic)
cvx.Problem.register_solve("admm", admm)
cvx.Problem.register_solve("admm2", admm2)
cvx.Problem.register_solve("consensus", admm_consensus)
cvx.Problem.register_solve("relax_and_round", relax_and_round)
cvx.Problem.register_solve("repeated_rr", repeated_rr)
