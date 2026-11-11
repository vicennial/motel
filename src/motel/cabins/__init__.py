"""motel cabins: disposable servers on an hourly lease. EXPERIMENTAL.

Not yet verified against a live provider. See docs/cabins.md.

Entry points:

* ``motel.cabins.cli:main(argv)``: the unprivileged ``motel cabin ...`` command (argv after ``cabin``).
* ``motel.cabins.desk:main(argv)``: the root front desk side, ``motel-desk cabin ...`` (argv after ``cabin``).
"""

STATUS = "experimental"
