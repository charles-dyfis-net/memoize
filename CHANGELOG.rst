1.1.3
-----

* Fixed declared supported python versions (`classifiers` in `setup.py`).

1.1.2
-----

* Added support for Python 3.10:
   * Applied workaround for testing setup (`MutableMapping` alias required by `tornado.testing.gen_test`);
   * Updated `mypy` and `coverage` setup to Python 3.10.
* Added support for Python 3.11 (provisional as only alpha releases of 3.11 were tested).

1.1.1
-----

* Fixed parallel queries returning expired entries (while expired entry was being refreshed).

1.1.0
-----

* Added support for manual invalidation.
* Added customization of basic params for DefaultInMemoryCacheConfiguration.
* Included initial mypy checking (tornado/asyncio interoperability forces some ignores).

1.0.4
-----

* Added support for Python 3.9.

1.0.3
-----

* Fixed unhandled KeyError in UpdateStatuses.

1.0.2
-----

* Added support for Python 3.8.

1.0.1
-----

* Removed unintended dependency on tornado (it prevented asyncio mode working without tornado installed).

1.0.0
-----

* Initial public release.
