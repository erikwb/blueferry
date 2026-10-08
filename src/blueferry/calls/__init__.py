"""Optional, default-off phone calls through oFono's HFP hands-free modem.

BlueFerry does not implement HFP itself. When the user switches calls on (Qt
settings, ``blueferry calls enable``, or ``BLUEFERRY_CALLS_ENABLED`` as the
initial value), the daemon observes the iPhone's oFono modem on the system bus and
exposes call state through the private ``Calls1`` session interface. oFono is
an optional runtime service: when it is missing the feature reports itself
unavailable and the rest of the daemon is unaffected.

``settings`` holds the saved opt-in, ``model`` pure parsing and validation,
``ofono`` is the thin system-bus transport, and ``controller`` owns
discovery, modem bring-up, and call state.
"""
