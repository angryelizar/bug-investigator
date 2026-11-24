# The agent itself. Builds of the Target Repository run on the host's Docker daemon
# through the mounted socket (see docker-compose.yml), so only the Docker CLI is here.
# trixie, not bookworm: fff-search ships only manylinux_2_38 wheels (glibc 2.38+).
FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim

# git and gh: the graph clones, pushes and talks to GitHub through them.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && apt-get purge -y curl && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI with buildx: executor.py parses BuildKit's `--progress=plain` log.
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker:27-cli /usr/local/libexec/docker/cli-plugins/docker-buildx \
    /usr/local/libexec/docker/cli-plugins/docker-buildx

# `git push` over https takes the token from gh, which reads GH_TOKEN from the env.
RUN git config --system credential.https://github.com.helper "" \
    && git config --system --add credential.https://github.com.helper "!gh auth git-credential"

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["chainlit", "run", "src/bug_investigator/ui.py", "--host", "0.0.0.0", "--port", "8000", "--headless"]
