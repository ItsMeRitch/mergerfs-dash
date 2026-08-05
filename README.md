# mergerfs-dash

A tiny, zero-dependency web dashboard that shows how your data is distributed
across the branches of a [mergerfs](https://github.com/trapexit/mergerfs) pool.

- Used/total/free per branch, plus a balance meter (fill-% spread)
- "Next write goes to" panel — the branch your mergerfs create policy would
  pick right now (reads the live policy from mergerfs in host mode)
- Which branch each top-level share's bytes actually live on
- Breakdown by file type (video / audio / images / docs / archives / other)
- Largest files in the pool and per-branch file/dir/inode counts
- **Zero config in Docker:** auto-discovers every mounted disk you map in;
  on the host it reads the branch list straight from mergerfs itself
- Deep stats come from a background scan that runs **once**, is cached to
  disk, and only re-runs when you press **Rescan** in the UI

The app is a single Python file using only the standard library. It is
completely read-only.

## Run with Docker (recommended)

A prebuilt multi-arch image (`linux/amd64` + `linux/arm64`) is available at
`ghcr.io/itsmeritch/mergerfs-dash`. On your server:

```bash
# copy compose.example.yml to your server as docker-compose.yml,
# edit the one host path (the parent of your disk mounts), then:
docker compose pull
docker compose up -d
# dashboard: http://your-server:8282
```

or directly:

```bash
docker run -d --name mergerfs-dash --restart unless-stopped -p 8282:8282 \
  -v /mnt:/branches:ro -v mergerfs-dash-data:/data \
  ghcr.io/itsmeritch/mergerfs-dash:latest
```

## Run with Portainer

> **You only map the branches — never the mergerfs pool mount itself.**
> The merged pool mount is *not* needed by the container; the app reads the
> underlying branches directly, as plain directories.
>
> **Zero config:** map the parent dir that holds your disk mounts, read-only,
> and every mounted filesystem found inside becomes a branch automatically.
> No environment variables needed.

1. In Portainer: **Stacks → Add stack**, name it `mergerfs-dash`.
2. Paste this into the web editor:

   ```yaml
   services:
     mergerfs-dash:
       image: ghcr.io/itsmeritch/mergerfs-dash:latest
       container_name: mergerfs-dash
       ports:
         - "8282:8282"
       volumes:
         - /mnt:/branches:ro            # ← the parent of your disk mounts
         - mergerfs-dash-data:/data
       restart: unless-stopped

   volumes:
     mergerfs-dash-data:
   ```

3. Edit the one host path (`/mnt`) to wherever your disks are mounted, and
   note:
   - `:ro` keeps the mapping **read-only** — the app never writes to your data.
   - Only mounts that exist **when the container starts** are discovered;
     if you add or remount a disk, restart/redeploy the container.
   - Keep the `mergerfs-dash-data:/data` volume: Portainer creates it
     automatically, and it persists the scan cache between restarts so the
     app doesn't rescan when the container restarts.
   - Optional: set env `BRANCHES` to a glob (`/branches/disk*`) or a
     comma-separated list to show only *some* of the discovered mounts.
4. **Deploy the stack**, then open `http://<your-server-ip>:8282`.

Notes:

- If the branch table shows numbers in the **Unreadable** column, some dirs
  (e.g. root-only `lost+found`) couldn't be counted; add `user: "0:0"`
  under the service and redeploy to include them (mounts stay read-only).
- To pick up a new release later: **Stacks → mergerfs-dash → Editor** →
  enable **"Re-pull image"** → **Update the stack**.

### Manual container (Add container form, no stack)

If you prefer **Containers → Add container**, it maps to the same fields:

1. **Name:** `mergerfs-dash`
   **Image:** `ghcr.io/itsmeritch/mergerfs-dash:latest`
2. **Manual network port publishing → publish a new network port:**
   host `8282` → container `8282` (TCP).
3. Scroll to **Advanced container settings → Volumes tab → Map additional
   volume** — two rows:

   | Host (volume field)      | Container path    | Writable? |
   |--------------------------|-------------------|-----------|
   | `/mnt` *(the parent of your disk mounts)* | `/branches` | **off** ❌ |
   | `mergerfs-dash-data` *(type: Volume, via the volume-picker icon)* | `/data` | **on** ✅ |

   The **Writable toggle is the UI equivalent of `:ro`** — keep it **off**
   for the data mapping, so the app can never modify your data. `/data` is
   the only writable path; it holds the scan cache so restarts don't rescan.
4. **No environment variables needed** — every mount under `/branches` is
   discovered automatically. (Optional: `BRANCHES=/branches/disk*` to
   restrict which discovered mounts are shown.)
5. **Restart policy tab:** `Unless stopped`.
6. **Deploy the container**, then open `http://<your-server-ip>:8282`.

Same permission note applies: if the **Unreadable** column shows numbers,
edit the container → **Commands & logging tab** → set **User** to `root` →
recreate (the data mapping stays read-only).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "Branches: 1", your disks appear in the *share distribution* panel, and pool capacity ≈ your server's system disk | You mapped the parent dir and pointed `BRANCHES` at it — the app treats it as a single branch | Remove `BRANCHES` entirely (auto-discovery), or point it at the disks themselves (`/branches/disks/disk*`) |

## Run without Docker

```bash
python3 mergerfs_dash.py --mount /storage
# or with explicit branches:
python3 mergerfs_dash.py --branches /mnt/disk1,/mnt/disk2 --port 8282
```

A systemd unit example is in the header comment of `mergerfs_dash.py`.

## Configuration

| Setting    | CLI flag      | Env var      | Default                                |
|------------|---------------|--------------|----------------------------------------|
| Branches   | `--branches`  | `BRANCHES`   | comma list or glob (`/branches/disk*`) — optional override |
| Mount      | `--mount`     | `MOUNT`      | host mode: auto-detect branches via mergerfs xattr         |
| Discovery  | `--discover-root` | `DISCOVERY_ROOT` | `/branches` — where mounted disks are found in zero-config mode |
| Port       | `--port`      | `PORT`       | `8282`                                 |
| Create policy | `--policy` | `CREATE_POLICY` | auto-read from mergerfs in host mode; in Docker it assumes `mfs` unless set |
| Listen addr| `--host`      | `HOST`       | `0.0.0.0`                              |
| Scan cache | `--cache`     | `CACHE_PATH` | next to the script / `/data/…` in Docker |

Detection order: `BRANCHES` → `MOUNT` xattr → any mergerfs entry in
`/proc/mounts` → auto-discovery under `DISCOVERY_ROOT`.
