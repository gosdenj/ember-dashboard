# Deploying with Portainer

Ember runs as a **stack** — a Docker Compose file that Portainer deploys
and keeps up to date for you.

```
  ┌──────────────────┐    HTTP     ┌───────────────────────┐    HTTPS    ┌───────────────┐
  │  the 7" panel    │ ──────────► │  the stack            │ ──────────► │ Hive          │
  │  (kiosk browser) │ ◄────────── │  on the target host   │ ◄─────────► │ Octopus       │
  └──────────────────┘   /events   └───────────────────────┘             │ Open-Meteo   │
                                      │  reads and writes                   │ postcodes.io │
                                      ▼                                    └───────────────┘
                              the ember-data volume
                            (the settings, the history
                             and the Hive session)
```

There are three pieces, and it is worth knowing which is which:

| Piece | Where it lives | Who changes it |
|---|---|---|
| **The image** | GitHub Container Registry | The GitHub workflow, on every push |
| **The stack** | `docker-compose.portainer.yml` in the repository | You, in a commit |
| **The settings** | The `ember-data` Docker volume | You, on the settings page |

## The thing that catches people out

**Portainer redeploys a stack without rebuilding it.** Under the hood it
runs `docker compose up -d` — with no `--build`. So a redeploy updates
the *compose file* but keeps the *image it already has*, and a new
commit would not reach the running container.

That is why the image comes from the registry rather than from the
repository:

```
   push to main
        │
        ▼
   the GitHub workflow builds the image ──► ghcr.io/…/ember-dashboard:latest
        │
        ▼
   Portainer notices the repository has changed, re-pulls the image
        │
        ▼
   the new version is running
```

The workflow is [`.github/workflows/ci.yml`](../.github/workflows/ci.yml)
and it is already part of the repository. It signs in with the workflow's
own token, so there is no secret to set up.

### The order matters

Portainer notices that the *repository* has changed, and redeploys the
stack — re-pulling the image as it does. If it did that while the image
was still being built, it would find the old image, deploy it, and then
have no reason to look again until the next commit. The new code would
sit there unpublished until someone happened to commit again.

So the workflow makes one small commit **after** the image has been
pushed:

```
   push to main
        │
        ▼
   the workflow builds the image and pushes it to the registry
        │
        ▼
   the workflow commits "publish <sha> [skip ci]"    ← this is the signal
        │
        ▼
   Portainer polls, sees the change, re-pulls the image, restarts
        │
        ▼
   the new version is running
```

`[skip ci]` is there so that commit does not start the workflow all over
again. It is an empty commit — nothing in it but the message.

If Portainer does happen to poll in the window before the image is
ready, it redeploys the old image and then redeploys again when the
signal commit arrives, so the pipeline heals itself either way.

### A push that does not start the workflow

**Not every push starts a workflow.** A push made with an OAuth token —
the kind `gh auth login` and many editors put in the keychain — does
not, and the image for that commit is then never built. Nothing says
so: the push succeeds, the repository moves, and the image simply never
appears. The dashboard sits on the old version until somebody happens
to commit again.

A **classic personal access token** does start the workflow, with the
`repo` scope.

Either way the workflow does not depend on it any more:

- it asks the registry whether the image for this commit is already
  published, and does nothing if it is
- it also runs **every four hours**, which catches the push that did
  not trigger it

So a missed trigger costs a few hours, not forever.

### Polling, and why not a webhook

Portainer can also be told to redeploy on demand through a **webhook**,
which is the tidier mechanism — a workflow calls it the moment the image
is published, and there is no polling at all.

A webhook does not work here, though. The target is on a private
network: `minix.local` is a LAN address, and the runner that builds the
image is on the internet and cannot reach it. The workflow can push to
the registry, because the registry is public; it cannot call a webhook
on someone's home network.

So the stack polls every five minutes instead, and the signal commit is
what tells it there is something to fetch.

## Before you start

The repository is public and the image in the registry is public too, so
there is no credential to set up. Portainer clones the repository
anonymously and pulls the image anonymously.

If you would rather keep either private:

| What | Where the credential goes |
|---|---|
| Cloning the repository | The stack's **Authentication → Basic** fields (a GitHub token) |
| Pulling the image | A **registry** in Portainer, if the package is private |

A **GitHub token** (classic) with:

- `repo` — to clone the private repository
- `read:packages` — to pull the image, if the package is private

A **deploy key** (an SSH key) works for the clone instead of a token,
and is tidier: it is scoped to this one repository. Portainer's git stack
takes a private key.

## 1. The stack

**Stacks → Add stack → Git repository**

| Field | Value |
|---|---|
| Name | `ember` |
| Repository URL | `https://github.com/gosdenj/ember-dashboard.git` |
| Repository reference name | `refs/heads/main` |
| Compose path | `docker-compose.portainer.yml` |
| Authentication | none needed — the repository is public |
| Environment variables | `EMBER_PORT` if the host port should not be 8766 |
| **Pull latest image** | **on** |
| **Poll repository for changes** | **on**, every 5 minutes |

The last two are what make it automatic. *Pull latest image* is the
re-pull of the registry image; without it, Portainer would redeploy the
old image and nothing would change.

Deploy it. The first deployment pulls the image, creates the volume and
starts the container.

### The same thing over the API

```bash
curl -X POST "http://minix.local:9000/api/stacks?type=2&method=repository&endpointId=2" \
  -H "X-API-Key: $PORTAINER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "Name": "ember",
    "RepositoryURL": "https://github.com/gosdenj/ember-dashboard.git",
    "RepositoryReferenceName": "refs/heads/main",
    "RepositoryAuthentication": false,
    "ComposeFile": "docker-compose.portainer.yml",
    "AutoUpdate": {
      "Interval": "5m",
      "ForceUpdate": false,
      "ForcePullImage": true
    },
    "Env": []
  }'
```

`type=2` is a Docker Compose stack, `method=repository` says the stack
comes from a git repository, and `endpointId=2` is the **minix**
environment in **Endpoints**.

## 2. The settings

The settings file — the Hive login and the Octopus API key — lives in a
**named Docker volume**, `ember-data`, mounted at `/data`. So do the two
other things that have to survive a redeploy:

| File in the volume | What it is |
|---|---|
| `settings.json` | the settings, including the secrets. Written `0600` |
| `hive-session.json` | the live Hive session, which renews itself |
| `history.db` | the gas readings and the heating polls |

A volume rather than a folder on the host, for three reasons:

- it is the **same** wherever the container runs, with no path to get wrong
- it **survives a redeploy** — a redeploy replaces the container, not the
  volume
- it does not depend on the host and the container sharing a filesystem,
  which they do not if Portainer is a container with Docker Desktop
  underneath it

The volume takes its ownership from the image, where `/data` belongs to
uid 1000 — the uid the container runs as. So Ember can write to it
without anybody having to `chown` anything.

### Setting it up in the browser

Open **http://<target-ip>:8766/settings** and paste in the two things
you actually have: your Hive login and your Octopus API key. Ember works
out the account, the meter, the tariff and the rates for itself, and
looks up your postcode.

**Hive sends a one-time code.** When it does, a box appears on the page
and you type the code in. The code only exists on your phone, so there
is no way around asking for it. Once the sign-in has worked, the session
is written into the volume and renews itself from then on.

The page also shows **Stored history**: how big the database has got,
how many rows of what are in it, how fast it is growing and how long it
is kept for. That is the answer to "is this going to fill the card?",
with a number rather than a feeling.

### If the volume ever gets the wrong ownership

```bash
docker run --rm -u 0 -v ember-data:/data alpine chown -R 1000:1000 /data
```

## 3. The updates

With polling on, Portainer checks the repository every five minutes. When
the commit has changed, it pulls the new image and recreates the
container.

There is **no rollback**. That is the one thing Portainer does not do. If
a new version breaks the dashboard, the way back is to check out the
last good commit and let Portainer redeploy it:

```bash
git revert <the bad commit>
git push
```

or, from the stack's page, **Pull and redeploy** after moving `main` back
to the last good commit.

## 4. The panel

The 7" screen is a browser in kiosk mode:

```bash
chromium --kiosk --noerrdialogs --disable-infobars --disable-pinch \
         --overscroll-history-navigation=0 http://<target-ip>:8766/
```

## The catch

**The dashboard has no authentication.** Anything that can reach the port
can read the page, change the settings, and read the Hive session.

On a box with a public address, do not publish the port. Keep it on the
LAN, put a password in front of it, or reach it over SSH:

```bash
ssh -L 8766:127.0.0.1:8766 you@your-server
```

For a hostname, add a site block to the `caddy` stack:

```
ember.longie.net {
    reverse_proxy ember:8766
}
```

Caddy reaches the dashboard by service name only if the dashboard
container joins the same Docker network the caddy container is on —
`proxy_net` in the other stacks. The stack here does not join it,
because that network is created by the caddy stack and a stack
declaring it as `external` cannot be deployed to a host where it does
not exist. If you want the hostname, add the network to the compose
file **and** make sure it is already there on the host before
redeploying:

```yaml
    networks:
      - proxy_net

networks:
  proxy_net:
    name: proxy_net
    external: true
```

## When something goes wrong

**The stack will not deploy** — the container's logs say why. The usual
one is the image: `docker pull ghcr.io/gosdenj/ember-dashboard:latest` on
the target will say whether it is a credentials problem or a missing
package.

**The settings will not save** — the container cannot write to the
volume. See *If the volume ever gets the wrong ownership* above.

**The dashboard is empty** — no credentials yet, or the Hive session has
died. The settings page says which; `docker logs ember` says why.

**A new commit did not arrive** — check that the workflow ran (the
*Actions* tab in the repository), that the image was published (the
*Packages* tab), and that *Pull latest image* is on in the stack.
