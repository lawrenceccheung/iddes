# ========================================================================
#
# Imports
#
# ========================================================================
import argparse
import os
import numpy as np
import scipy.spatial.qhull as qhull
import pandas as pd
from mpi4py import MPI
import stk
from scipy.interpolate import griddata

# ========================================================================
#
# Define constants
#
# ========================================================================
channel_xplanes = [2, 4, 6]
dx              = 0.05
ninterp         = 201
interiorname    = "fluid-HEX"  # "interior-hex"

# ========================================================================
#
# Functions
#
# ========================================================================
def p0_printer(par):
    iproc = par.rank

    def printer(*args, **kwargs):
        if iproc == 0:
            print(*args, **kwargs)

    return printer


# ========================================================================
def interp_weights(xyz, uvw):
    """Find the interpolation weights

    See: https://stackoverflow.com/questions/20915502/speedup-scipy-griddata-for-multiple-interpolations-between-two-irregular-grids
    """
    d = 3
    tri = qhull.Delaunay(xyz)
    simplex = tri.find_simplex(uvw)
    vertices = np.take(tri.simplices, simplex, axis=0)
    temp = np.take(tri.transform, simplex, axis=0)
    delta = uvw - temp[:, d]
    bary = np.einsum("njk,nk->nj", temp[:, :d, :], delta)
    return vertices, np.hstack((bary, 1 - bary.sum(axis=1, keepdims=True)))


# ========================================================================
def interpolate(values, vtx, wts):
    return np.einsum("nj,nj->n", np.take(values, vtx), wts)


# ========================================================================
#
# Main
#
# ========================================================================
if __name__ == "__main__":

    # Parse arguments
    parser = argparse.ArgumentParser(description="A simple post-processing tool")
    parser.add_argument(
        "-m",
        "--mfile",
        help="Root name of files to postprocess",
        required=True,
        type=str,
    )
    parser.add_argument("--auto_decomp", help="Auto-decomposition", action="store_true")
    parser.add_argument(
        "-v",
        "--vel_name",
        help="Name of the velocity field",
        default="velocity",
        type=str,
    )
    parser.add_argument(
        "-navg", help="Number of times to average", default=10, type=int
    )
    parser.add_argument(
        "--flowthrough", help="Flowthrough time (L/u)", default=0.4, type=float
    )
    parser.add_argument(
        "--factor",
        help="Factor of flowthrough time between time steps used in average",
        type=float,
        default=1.2,
    )
    args = parser.parse_args()

    fdir = os.path.dirname(args.mfile)

    comm = MPI.COMM_WORLD
    size = comm.Get_size()
    rank = comm.Get_rank()
    par = stk.Parallel.initialize()
    printer = p0_printer(par)

    mesh = stk.StkMesh(par)
    printer("Reading meta data for mesh: ", args.mfile)
    mesh.read_mesh_meta_data(args.mfile, auto_decomp=args.auto_decomp)
    printer("Done reading meta data")

    printer("Loading bulk data for mesh: ", args.mfile)
    mesh.populate_bulk_data()
    printer("Done reading bulk data")

    num_time_steps = mesh.stkio.num_time_steps
    max_time = mesh.stkio.max_time
    tsteps = np.array(mesh.stkio.time_steps)
    printer(f"""Num. time steps = {num_time_steps}\nMax. time step  = {max_time}""")

    # Figure out the times over which to average
    tmp_tavg = np.sort(
        tsteps[-1] - args.flowthrough * args.factor * np.arange(args.navg)
    )
    dist = np.abs(np.array(tsteps)[:, np.newaxis] - tmp_tavg)
    idx = dist.argmin(axis=0)
    tavg = tsteps[idx]
    tavg_instantaneous = tsteps[idx[0] :]
    printer("Averaging the following steps:")
    printer(tavg)

    # Extract time and spanwise average tau_wall on wall
    tw_data = None
    for tstep in tavg_instantaneous:
        ftime, missing = mesh.stkio.read_defined_input_fields(tstep)
        printer(f"Loading tau_wall fields for time: {ftime}")

        coords = mesh.meta.coordinate_field
        wall = mesh.meta.get_part("wall_bottom")
        sel = wall & mesh.meta.locally_owned_part
        tauw = mesh.meta.get_field("tau_wall")
        names = ["x", "y", "z", "tauw"]
        nnodes = sum(bkt.size for bkt in mesh.iter_buckets(sel, stk.StkRank.NODE_RANK))

        cnt = 0
        data = np.zeros((nnodes, len(names)))
        for bkt in mesh.iter_buckets(sel, stk.StkRank.NODE_RANK):
            xyz = coords.bkt_view(bkt)
            tw = tauw.bkt_view(bkt)
            data[cnt : cnt + bkt.size, :] = np.hstack((xyz, tw.reshape(-1, 1)))
            cnt += bkt.size

        if tw_data is None:
            tw_data = np.zeros(data.shape)
        tw_data += data / len(tavg_instantaneous)

    lst = comm.gather(tw_data, root=0)
    comm.Barrier()
    if rank == 0:
        df = pd.DataFrame(np.vstack(lst), columns=names)
        tw = df.groupby("x", as_index=False).mean().sort_values(by=["x"])
        twname = os.path.join(fdir, "tw.dat")
        tw.to_csv(twname, index=False)

    # Extract (average) velocity data
    vel_data = None
    for tstep in tavg:
        ftime, missing = mesh.stkio.read_defined_input_fields(tstep)
        printer(f"Loading {args.vel_name} fields for time: {ftime}")

        interior = mesh.meta.get_part(interiorname)
        sel = interior & mesh.meta.locally_owned_part
        velocity = mesh.meta.get_field(args.vel_name)
        names = ["x", "y", "z", "u", "v", "w"]
        nnodes = sum(bkt.size for bkt in mesh.iter_buckets(sel, stk.StkRank.NODE_RANK))

        cnt = 0
        data = np.zeros((nnodes, len(names)))
        for bkt in mesh.iter_buckets(sel, stk.StkRank.NODE_RANK):
            xyz = coords.bkt_view(bkt)
            vel = velocity.bkt_view(bkt)
            data[cnt : cnt + bkt.size, :] = np.hstack((xyz, vel))
            cnt += bkt.size

        if vel_data is None:
            vel_data = np.zeros(data.shape)
        vel_data += data / len(tavg)

    # Subset the velocities on planes
    #dx = 0.05 * 4
    planes = []
    #for x in utilities.xplanes():
    for x in channel_xplanes:
        print(" === x=%f ==="%x)
        # subset the data around the plane of interest
        sub = vel_data[(x - dx <= vel_data[:, 0]) & (vel_data[:, 0] <= x + dx), :]

        lst = comm.gather(sub, root=0)

        comm.Barrier()
        if rank == 0:
            xi = np.array([x])
            df = (
                pd.DataFrame(np.vstack(lst), columns=names)
                .groupby(["x", "z"], as_index=False)
                .mean()
                .sort_values(by=["x", "z"])
            )
            zmin, zmax = df.z.min(), df.z.max() #utilities.hill(xi)[0], df.y.max()
            #print("zmin, zmax = %e,%e"%(zmin, zmax))
            zi = np.linspace(zmin, zmax, ninterp)

            umean = griddata(
                (df.x, df.z), df.u, (xi[None, :], zi[:, None]), method="cubic"
            ).flatten()
            vmean = griddata(
                (df.x, df.z), df.v, (xi[None, :], zi[:, None]), method="cubic"
            ).flatten()
            wmean = griddata(
                (df.x, df.z), df.w, (xi[None, :], zi[:, None]), method="cubic"
            ).flatten()

            # Old way:
            # zi = np.unique(df.z)
            # xis = np.array(np.meshgrid(xi, yi, zi)).T.reshape(-1, 3)

            # # Equivalent to:
            # # ui = spi.griddata(
            # #     (df.x, df.y, df.z),
            # #     df.u,
            # #     (xi[None, None, :], yi[None, :, None], zi[:, None, None]),
            # #     method="linear",
            # # )
            # # but saves the wts for reuse
            # vtx, wts = interp_weights(df[["x", "y", "z"]].values, xis)
            # ui = interpolate(df.u.values, vtx, wts).reshape(-1, ninterp)
            # vi = interpolate(df.v.values, vtx, wts).reshape(-1, ninterp)
            # wi = interpolate(df.w.values, vtx, wts).reshape(-1, ninterp)
            # umean = np.mean(ui, axis=0).flatten()
            # vmean = np.mean(vi, axis=0).flatten()
            # wmean = np.mean(wi, axis=0).flatten()

            planes.append(
                pd.DataFrame(
                    {
                        "x": x * np.ones(zi.shape),
                        "z": zi,
                        "u": umean,
                        "v": vmean,
                        "w": wmean,
                    }
                )
            )

    if rank == 0:
        df = pd.concat(planes)
        df.to_csv(os.path.join(fdir, "profiles.dat"), index=False)
