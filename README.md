# mergerfs-dash

A tiny, zero-dependency web dashboard that shows how your data is distributed
across the branches of a [mergerfs](https://github.com/trapexit/mergerfs) pool.

- Used/total/free per branch, plus a balance meter (fill-% spread)
- Which branch each top-level share's bytes actually live on
- Breakdown by file type (video / audio / images / docs / archives / other)
- Largest files in the pool and per-branch file/dir/inode counts
- Deep stats come from a background scan that runs **once**, is cached to
  disk, and only re-runs when you press **Rescan** in the UI

The app is a single Python file using only the standard library. It is
completely read-only.

## Run with Docker (recommended)

A prebuilt multi-arch image (`linux/amd64` + `linux/arm64`) is available at
`ghcr.io/itsmeritch/mergerfs-dash`. On your server:

```bash
# copy compose.example.yml to your server as docker-compose.yml,
# edit the branch paths to match your pool, then:
docker compose pull
docker compose up -d
# dashboard: http://your-server:8282
```

## Run with Portainer

1. In Portainer: **Stacks → Add stack**, name it `mergerfs-dash`.
2. Paste this into the web editor:

   ```yaml
   services:
     mergerfs-dash:
       image: ghcr.io/itsmeritch/mergerfs-dash:latest
       container_name: mergerfs-dash
       ports:
         - "8282:8282"
       environment:
         BRANCHES: /branches/disk1,/branches/disk2
       volumes:
         - /mnt/disk1:/branches/disk1:ro
         - /mnt/disk2:/branches/disk2:ro
         - mergerfs-dash-data:/data
       restart: unless-stopped

   volumes:
     mergerfs-dash-data:
   ```

3. Edit it to match your pool:
   - **One volume line per branch.** Left side = the real path on the host
     (e.g. `/mnt/disk1`), right side = any path the app will see inside the
     container. The `:ro` at the end is important — it mounts each branch
     **read-only**; the app never writes to your data.
   - **`BRANCHES` lists the container-side paths** (the right sides),
     comma-separated, one per branch, in the same order as the volume lines.
   - Keep the `mergerfs-dash-data:/data` volume: Portainer creates it
     automatically, and it persists the scan cache between restarts so the
     app doesn't rescan when the container restarts.
4. **Deploy the stack**, then open `http://<your-server-ip>:8282`.

Notes:

- If the dashboard's scan reports lots of "permission denied" errors, add
  `user: "0:0"` under the service and redeploy (the mounts stay read-only).
- To pick up a new release later: **Stacks → mergerfs-dash → Editor** →
  enable **"Re-pull image"** → **Update the stack**.

### Manual container (Add container form, no stack)

If you prefer **Containers → Add container**, it maps to the same fields:

1. **Name:** `mergerfs-dash`
   **Image:** `ghcr.io/itsmeritch/mergerfs-dash:latest`
2. **Manual network port publishing → publish a new network port:**
   host `8282` → container `8282` (TCP).
3. Scroll to **Advanced container settings → Volumes tab → Map additional
   volume** — one row per branch, plus one for the cache:

   | Host (volume field)      | Container path    | Writable? |
   |--------------------------|-------------------|-----------|
   | `/mnt/disk1`             | `/branches/disk1` | **off** ❌ |
   | `/mnt/disk2`             | `/branches/disk2` | **off** ❌ |
   | `mergerfs-dash-data` *(type: Volume, via the volume-picker icon)* | `/data` | **on** ✅ |

   The **Writable toggle is the UI equivalent of `:ro`** — switch it **off**
   for every branch, so the app can never modify your data. `/data` is the
   only writable path; it holds the scan cache so restarts don't rescan.
4. **Env tab → Add environment variable:**
   name `BRANCHES`, value `/branches/disk1,/branches/disk2`
   (the container paths, comma-separated, same order as the volume rows).
5. **Restart policy tab:** `Unless stopped`.
6. **Deploy the container**, then open `http://<your-server-ip>:8282`.

Same permission note applies: if the scan reports "permission denied",
edit the container → **Commands & logging tab** → set **User** to `root` →
recreate (the branch mounts stay read-only).

```bash
python3 mergerfs_dash.py --mount /storage
# or with explicit branches:
python3 mergerfs_dash.py --branches /mnt/disk1,/mnt/disk2 --port 8282
```

A systemd unit example is in the header comment of `mergerfs_dash.py`.

## Configuration

| Setting    | CLI flag      | Env var      | Default                                |
|------------|---------------|--------------|----------------------------------------|
| Branches   | `--branches`  | `BRANCHES`   | auto-detected from `--mount` via xattr |
| Mount      | `--mount`     | `MOUNT`      | —                                      |
| Port       | `--port`      | `PORT`       | `8282`                                 |
| Listen addr| `--host`      | `HOST`       | `0.0.0.0`                              |
| Scan cache | `--cache`     | `CACHE_PATH` | next to the script / `/data/…` in Docker |
