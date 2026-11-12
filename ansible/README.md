# The development machine, as a playbook

An Ansible playbook that sets up the command-line tools of this project on **your own machine** (`localhost`, no SSH):
the same list as the runbook in [`../local/README.md`](../local/README.md), written as code, so a second machine gets the
same versions. It installs tools at pinned versions and ends with read-only checks. It **never deploys anything into
Kubernetes**: the cluster is reached by each service's pipeline with Helm, not from here, and nothing in this folder
creates, changes or deletes a cluster, an Azure or a GitHub resource.

## What it installs

| Tag | Tool | Version | From | Checked by |
|---|---|---|---|---|
| `node` | Node.js and npm | 24.22.2 | the tarball of nodejs.org | `SHASUMS256.txt` of the release |
| `docker` | Docker Engine, CLI, containerd, buildx, compose plugin | 29, 2, 0, 5 (the major versions) | Docker's apt repository (Debian, Ubuntu); macOS: Docker Desktop by hand | the repository key in `files/docker.asc` |
| `kubectl` | kubectl | 1.35.10 | dl.k8s.io | the `.sha256` next to the binary |
| `helm` | Helm | 4.3.1 | get.helm.sh | the `.sha256sum` next to the tarball |
| `kubeconform` | kubeconform | 0.8.0 | the GitHub release | the release's `CHECKSUMS` |
| `azure-cli` | Azure CLI | 2.92.0 | Microsoft's apt repository (Debian, Ubuntu); macOS: `brew install azure-cli` by hand | the repository key in `files/microsoft.asc` |
| `terraform` | Terraform | 1.17.1 | releases.hashicorp.com | the release's `SHA256SUMS` |
| `kind` | kind (optional, off by default) | 0.33.0 | the GitHub release | the release's `.sha256sum` |
| `checks` | the read-only checks | | | |

All versions are in [`vars/versions.yml`](vars/versions.yml), and only there. The download tools go into
`/usr/local/lib/rso-tools/<tool>/<version>/` and are linked into `/usr/local/bin`, so a new version never overwrites the
old one and going back is one changed variable. Nothing is fetched when the pinned version is already there. The
downloads are kept in `~/.cache/rso-dev-setup` (safe to delete).

## What it needs

- Linux (Debian or Ubuntu, also in WSL) or macOS, on x86_64 or arm64. Another distribution is refused at the start with a
  message: the apt repositories of Docker and Microsoft are written for Debian and Ubuntu.
- Python 3.12 or newer for ansible-core 2.21 (Ubuntu 24.04 has it), and on Debian and Ubuntu the system `python3-apt`
  (installed by default on a desktop; in a minimal image `sudo apt install python3-apt`).
- `sudo` for the packages and for `/usr/local` (the password is asked with `--ask-become-pass` and never stored), and a
  network connection.
- macOS: **untested**. GNU tar is needed (`brew install gnu-tar`; the module that unpacks the archives refuses the bsdtar
  of macOS, and the playbook stops at the first archive with that message). Docker Desktop and the Azure CLI are left to
  you.
- WSL with Docker Desktop on Windows: turn on its WSL integration and skip the Docker tag (`--skip-tags docker`).

## Run it

Everything runs from this folder, because Ansible reads `ansible.cfg` only from the current folder.

```bash
cd infrastructure/ansible
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt     # ansible-core 2.21.6 and ansible-lint 26.11.0
source .venv/bin/activate

ansible-playbook site.yml --syntax-check
ansible-lint                                                           # profile production (.ansible-lint)

# 1. The dry run: changes nothing. Read what it reports.
ansible-playbook site.yml --check --diff --ask-become-pass
# 2. Then for real.
ansible-playbook site.yml --ask-become-pass
```

A second real run should report `changed=0` (the design says so; it was not run, see the last section). More ways to run it:

```bash
ansible-playbook site.yml --ask-become-pass --tags helm,kubeconform    # only some tools (the tags of the table)
ansible-playbook site.yml --ask-become-pass --skip-tags docker         # everything but Docker (WSL with Docker Desktop)
ansible-playbook site.yml --ask-become-pass -e install_kind=true       # also kind (plan Q-17: only if it is asked for)
ansible-playbook site.yml --tags checks                                # the read-only checks alone: no password needed
```

### What the dry run shows, and what it cannot

- For a tool that is not installed yet it prints what a real run does (the URL, the checksum file, the folder, the
  links) and downloads nothing: the download needs a folder that a dry run does not create. For a tool that is
  installed it compares the links.
- The apt parts (Docker, Azure CLI) show the repository file that would be added. The packages are listed only once the
  repository is there, so a first dry run says that they wait for it.
- A program of the same name that is not a link made by this playbook (a `/usr/local/bin/helm` from Helm's own install
  script, for example) is reported in the dry run, and **stops** a real run before that tool is downloaded: move it
  aside (`sudo mv /usr/local/bin/helm /usr/local/bin/helm.old`) and run again. The playbook never deletes a program it
  did not install. A link that points somewhere else is replaced.
- The playbook does not remove a conflicting package (`docker.io`, `podman-docker`, the `docker-compose` of Ubuntu):
  apt stops with its own message and the decision is yours. It does not add you to the `docker` group (that group is
  root-equivalent); `-e docker_add_user_to_group=true` does.

## The checks at the end

They only read, report and never fail the run: the version of each tool, `docker info` (is the daemon reachable), whether
an Azure login is present (`az account show`, yes or no) and whether the current kubectl context reaches a cluster
(`kubectl cluster-info`, yes or no, bounded to 15 seconds on Linux). They run in a dry run too, because they change
nothing. They print no subscription, account or cluster address. A tool that is missing is reported with the tag that
installs it.

## Change a version

Edit [`vars/versions.yml`](vars/versions.yml), run the dry run, then the real run. The file is bumped by hand: Dependabot
does not read it. Terraform's version also lives in two other places that must move together with it:
`TERRAFORM_VERSION` in `.github/workflows/terraform.yml` and `required_version` in the `versions.tf` files. Docker's
pins are patterns (`5:29.*`, because Docker's package versions carry the name of the distribution); a new major version
is a decision, so you change the pattern.

## Trust

Every download is checked against the checksum the vendor publishes next to it (sha256, from the vendor's own host), and
the check fails when the file name is not in the list. The apt repositories are trusted through the keys kept in
`files/`, so no key is fetched while the playbook runs. Fingerprints, to compare with the vendors' pages: Docker
`9DC8 5822 9FC7 DD38 854A E2D8 8D81 803C 0EBF CD88`, Microsoft `BC52 8686 B50D 79E3 39D3 721C EB3E 94AD BE12 29CF`
(`gpg --show-keys files/docker.asc`). To renew a key, replace the file after comparing the fingerprint. The tarballs are
unpacked with `--no-same-owner`, so everything under `/usr/local/lib/rso-tools` belongs to root.

## What was checked, and what was not

Checked on the machine that wrote it: `ansible-playbook --syntax-check`, `ansible-lint` with the production profile, the
download URLs and the checksum files of the vendors (they exist for amd64 and arm64), and the install logic of one tool
(not installed, installed, a file in the way, a second run) against scratch folders. **Not checked:** a run of the
playbook itself, dry or real, on any machine; the apt repositories and package pins; macOS; the Azure and cluster checks.
Do the dry run first and read it.
