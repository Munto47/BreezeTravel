# Frontend immutable release

This document describes the build/runtime invariant only. The current project
status is `three_city_local_rc1_candidate`; it is not evidence of a deployed
public full-stack release or a passed three-city RC1 quality gate.

Production builds use Next.js `output: standalone`. Each container image owns
one complete `.next/standalone` and `.next/static` pair; files are never copied
into a directory used by a running `next start` process.

Release invariant:

1. build a new image tagged with the Git commit;
2. start it and pass the `/` health check;
3. switch traffic to the new container;
4. stop the old container only after the switch.

`frontend/Dockerfile` enforces the runtime half of this invariant by copying
only standalone output and starting `node server.js`.

The build also uses the local/system sans-serif stack. It does not download a
Google Font during `next build`, so a transient font CDN failure cannot create
a partially built release.
