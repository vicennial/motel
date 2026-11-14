# Security policy

## How do I report a vulnerability?

Use GitHub private vulnerability reporting: open the **Security** tab of
[the repository](https://github.com/vicennial/motel), choose **Report a vulnerability**, and describe the problem
(<https://github.com/vicennial/motel/security/advisories/new>). Only the maintainers see the report.

Please do not open a public issue or pull request for a vulnerability, and do not post a token, even a revoked one.
If you are not sure the problem is a vulnerability, report it privately anyway.

Include the motel version (`motel --version`), what you did, what you expected and what happened. A reproduction
against the fake or local provider is ideal. You do not need a cloud account to report a problem.

You can expect an acknowledgement within a week. This is a small project, so fixes and releases are best effort.
When a report is valid, a fixed release and a published advisory come together, and you are credited if you want to be.

## What is in scope?

motel runs a small helper as root (`motel-desk`, the front desk) that holds a cloud token. A flaw is in scope when it
lets a caller do more than the documented limits allow. Examples:

- running something as root, or reading the cloud token, through the sudo rule, an argument, a path, an environment
  variable or a symbolic link;
- making the front desk create, change or delete a cloud resource that motel did not create (a failure of the
  ownership guard or the never-touch set), or go over a cap;
- a cabin firewall, key or guest setting that is wider than documented, or a cabin that outlives its checkout time
  without being reported;
- a real API key, a cloud token or an own-login token that reaches a tour guest, a log, a result or a process list;
- the installer accepting a source that someone other than root can change;
- the MCP server or the Omnigent provider gaining a privilege the unprivileged `motel` command does not have;
- a way for one user to read, change or end another user's room, cabin or tour.

## What is out of scope?

These are documented limits, not vulnerabilities:

- A hostile root user on the host. Root can read the token.
- Anything a cabin's guest user does inside its own cabin. The guest user has sudo and outbound HTTP and HTTPS, so
  anything synced to a cabin can be copied out ([cabins: honest limits](docs/cabins.md#honest-limits)).
- A tour guest reading what you put in its repository bundle, or spending its own budget
  ([tours: what is not protected](docs/tours.md#what-is-not-protected)).
- Rooms. They are folders on your own host and are not a sandbox.
- Cloud costs from your own caps and configuration. The provider invoice is authoritative.
- Bugs in a cloud provider, Omnigent, or another dependency. Report those upstream.
- Behaviour of the beta providers that only a real account would show. Open a normal issue for that.

## Which versions get fixes?

The latest release on PyPI.
