"""direct-index: manage index investments by directly holding constituents.

The package is split into five modules mirroring the system's responsibilities:

* ``indexes``   -- fetch index constituents and target weights (module #1)
* ``broker``    -- read positions (#2) and submit trades (#3)
* ``rebalance`` -- blend many indexes into one target and diff vs. holdings (#4)
* ``tax``       -- cost-basis lot ledger with HIFO sell selection (#5)
* ``config``    -- local configuration tying the above together

The core (rebalance, tax, config, the paper broker and the CSV provider) has no
third-party dependencies and runs entirely offline. Interactive Brokers and
iShares are optional integrations layered on top of the same interfaces.
"""

__version__ = "0.1.0"
