"""meshcore imports pycryptodome as `Crypto`; Debian's python3-pycryptodome installs it as
`Cryptodome`. This package makes `Crypto.*` load from Debian's copy."""

import Cryptodome

__path__ = Cryptodome.__path__
__version__ = Cryptodome.__version__
version_info = Cryptodome.version_info
