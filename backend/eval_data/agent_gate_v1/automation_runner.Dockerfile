FROM node:20.18.1-bookworm-slim@sha256:b2c8e0eb8a6aeeae33b2711f8f516003e27ee45804e270468d937b3214f2f0cc AS node_runtime

# Candidate-controlled dependency graphs are resolved as an unprivileged user
# in a throw-away stage. They never execute with access to the final runner's
# root filesystem or its Python verifier environment.
FROM node_runtime AS candidate_dependencies
USER root
RUN mkdir -p /workspace/packages/trip-check-client /workspace/frontend /workspace/miniapp \
    && chown -R node:node /workspace
USER node
COPY --from=candidate_git --chown=node:node source/packages/trip-check-client/package.json source/packages/trip-check-client/package-lock.json /workspace/packages/trip-check-client/
RUN npm --prefix /workspace/packages/trip-check-client ci --ignore-scripts
COPY --from=candidate_git --chown=node:node source/frontend/package.json source/frontend/package-lock.json /workspace/frontend/
RUN npm --prefix /workspace/frontend ci --ignore-scripts
COPY --from=candidate_git --chown=node:node source/miniapp/package.json source/miniapp/package-lock.json source/miniapp/.npmrc /workspace/miniapp/
RUN npm --prefix /workspace/miniapp ci --ignore-scripts

FROM pgvector/pgvector:0.8.1-pg16@sha256:33198da2828a14c30348d2ccb4750833d5ed9a44c88d840a0e523d7417120337

COPY --from=node_runtime /usr/local/ /usr/local/

RUN rm -f /etc/apt/sources.list.d/* \
    && printf '%s\n' \
        'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20250820T000000Z bookworm main' \
        'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/20250820T000000Z bookworm-security main' \
        > /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && sed -i 's|http://snapshot.debian.org|https://snapshot.debian.org|g' \
        /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends \
        curl \
        git \
        python3 \
        python3-pip \
        python3-venv \
        redis-server \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/breezetravel-agent-gate-venv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONNOUSERSITE=1 \
    PATH=/opt/breezetravel-agent-gate-venv/bin:/usr/local/bin:/usr/bin:/bin \
    HOME=/tmp/breezetravel-agent-gate-home \
    XDG_CONFIG_HOME=/tmp/breezetravel-agent-gate-home/.config \
    NPM_CONFIG_USERCONFIG=/tmp/breezetravel-agent-gate-home/.npmrc \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /workspace
COPY --from=candidate_git source/backend/eval_data/agent_gate_v1/automation_runner_requirements.lock /tmp/backend-requirements.lock
RUN python -m pip install --no-cache-dir \
        --require-hashes \
        -r /tmp/backend-requirements.lock

# Browser installation is driven only by this authority-owned exact lock. No
# candidate package or binary executes while the image still runs as root.
COPY --from=candidate_git source/backend/eval_data/agent_gate_v1/automation_runner_browser_package.json /opt/breezetravel-agent-gate-browser/package.json
COPY --from=candidate_git source/backend/eval_data/agent_gate_v1/automation_runner_browser_package-lock.json /opt/breezetravel-agent-gate-browser/package-lock.json
RUN npm --prefix /opt/breezetravel-agent-gate-browser ci --ignore-scripts \
    && mkdir -p /ms-playwright \
    && /opt/breezetravel-agent-gate-browser/node_modules/.bin/playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright

COPY --from=candidate_git candidate.pack candidate.commit candidate.tree candidate.shallow /tmp/candidate-git/
COPY --from=candidate_git --chown=postgres:postgres source/ /workspace
COPY --from=candidate_dependencies --chown=postgres:postgres /workspace/packages/trip-check-client/node_modules/ /workspace/packages/trip-check-client/node_modules/
COPY --from=candidate_dependencies --chown=postgres:postgres /workspace/frontend/node_modules/ /workspace/frontend/node_modules/
COPY --from=candidate_dependencies --chown=postgres:postgres /workspace/miniapp/node_modules/ /workspace/miniapp/node_modules/
RUN git -C /workspace init --quiet \
    && git -C /workspace index-pack --stdin < /tmp/candidate-git/candidate.pack \
    && cp /tmp/candidate-git/candidate.shallow /workspace/.git/shallow \
    && candidate_commit="$(tr -d '\r\n' < /tmp/candidate-git/candidate.commit)" \
    && candidate_tree="$(tr -d '\r\n' < /tmp/candidate-git/candidate.tree)" \
    && git -C /workspace update-ref HEAD "${candidate_commit}" \
    && git -C /workspace reset --hard "${candidate_commit}" \
    && test "$(git -C /workspace rev-parse HEAD)" = "${candidate_commit}" \
    && test "$(git -C /workspace show -s --format=%T HEAD)" = "${candidate_tree}" \
    && rm -rf /tmp/candidate-git \
    && install -o postgres -g postgres -m 0755 \
        /workspace/backend/eval_data/agent_gate_v1/automation_runner_entrypoint.sh \
        /usr/local/bin/breezetravel-agent-gate-entrypoint \
    && mkdir -p /tmp/breezetravel-agent-gate-home \
    && chown -R postgres:postgres /workspace/.git \
    && chown postgres:postgres \
        /tmp/breezetravel-agent-gate-home \
        /workspace \
        /workspace/backend \
        /workspace/frontend \
        /workspace/miniapp \
        /workspace/packages \
        /workspace/packages/trip-check-client

USER postgres
ENTRYPOINT ["/usr/local/bin/breezetravel-agent-gate-entrypoint"]
