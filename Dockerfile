# The pipeline, running somewhere that is not Lyon's PC.
#
# Everything expensive -- finding leads, researching them, building their
# pages, writing the messages -- is work a server does well. The one thing
# that stays behind is sending: the WhatsApp relay needs a phone linked to a
# machine that stays awake, and running that in a data centre puts the number
# at more risk rather than less. A click-to-chat link replaces it, so nothing
# in this image needs Node at all.
FROM python:3.11-slim

# Chromium, for photographing each built page. The screenshotter drives
# whatever browser it finds on PATH rather than installing one, and
# "chromium" is already in the names it looks for -- Debian puts it at
# /usr/bin/chromium, so this needs no configuration.
#
# ca-certificates is not optional: every call this makes is https, and
# without it Google, Cloudflare and Anthropic all fail certificate
# verification with an error that reads like a network outage.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        chromium \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The source has to be here before the install: `packages.find` looks in
# src/, so installing with only pyproject.toml present would succeed and
# install nothing at all -- an image whose `outreach` command does not exist.
# Splitting this into a cached dependency layer is possible and was tried;
# it produced exactly that empty package, so correctness wins over the
# rebuild time.
COPY pyproject.toml ./
COPY src/ ./src/
COPY config/ ./config/
RUN pip install --no-cache-dir .

# Everything writable lives here, and nowhere else. The image is replaced on
# every deploy, so a Google token or a saved key written into /app would be
# gone at the next release -- and losing the token means signing in again
# with a browser this machine does not have.
ENV OUTREACH_STATE_DIR=/data
VOLUME ["/data"]

# Not root. A container that writes as root makes the mounted volume
# root-owned too, which is a repair job on the host rather than a setting.
RUN useradd --create-home --uid 10001 outreach \
    && mkdir -p /data \
    && chown -R outreach:outreach /data /app
USER outreach

# The platform decides the port; 8765 matches the local default so the same
# instructions work in both places.
ENV PORT=8765

# The morning batch is off unless asked for -- a scheduled run spends money,
# and nobody should learn that from a bill. Set OUTREACH_DAILY_RUN=07:30 in
# the host's environment to turn it on.
#
# TZ decides what "07:30" means. Without it a container is UTC, which is
# half past three in the afternoon in Penang -- the single most reliable way
# to schedule something for the wrong moment. The startup line prints the
# date, time and offset it actually computed, so there is nothing to argue
# about.
ENV TZ=UTC
EXPOSE 8765

# tini reaps the browser. Chromium leaves zombie children behind on a
# screenshot that timed out, and PID 1 in a container does not reap them by
# default -- a long-running dashboard would accumulate them until it could
# not fork.
ENTRYPOINT ["/usr/bin/tini", "--"]

# No --phone and no --host: `serve` binds every interface by itself when a
# state directory is set, and refuses to start at all without a password.
# The launcher has no decision to get wrong.
CMD ["sh", "-c", "outreach dashboard --port ${PORT}"]
