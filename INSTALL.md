Installing Promptline
=====================

Promptline isn't in distribution repositories yet. On Debian and Ubuntu,
install the `.deb` package from a release, or build it yourself. Otherwise,
run it from a checkout.

Debian / Ubuntu: from a release
-------------------------------

Download `promptline_<version>_all.deb` from the
[releases page](https://github.com/MurcusM/promptline/releases), then:

    sudo apt install ./promptline_<version>_all.deb

Each release also lists the package's SHA-256 checksum (`SHA256SUMS`).

Debian / Ubuntu: building the package
-------------------------------------

    sudo apt install debhelper dh-python gettext intltool   # build tools, once
    git clone https://github.com/MurcusM/promptline.git
    cd promptline
    dpkg-buildpackage -us -uc -b
    sudo apt install ../promptline_*_all.deb

apt installs the dependencies. You get the `promptline` command and a
Promptline entry in your applications menu. To make it your default
terminal:

    sudo update-alternatives --config x-terminal-emulator

On GNOME, also set it as the terminal the desktop opens (for example with
Ctrl+Alt+T) in Settings. To update, pull, rebuild and install the new
`.deb` the same way. To remove: `sudo apt remove promptline`.

Dependencies
------------

Debian/Ubuntu:

    sudo apt install python3-gi python3-gi-cairo python3-psutil python3-configobj \
      gir1.2-gtk-3.0 gir1.2-vte-2.91 gir1.2-keybinder-3.0 gir1.2-notify-0.7 \
      gettext intltool

Suggestions, prediction and `@agent` need a VTE with terminal-property
support (0.78 or newer). On an older VTE Promptline runs as plain Terminator.

From a checkout
---------------

    git clone https://github.com/MurcusM/promptline.git
    cd promptline
    python3 promptline

Installing with setup.py
------------------------

For systems without Debian packaging:

    python3 setup.py build
    python3 setup.py install --user --record=install-files.txt

This installs the `promptline`, `promptline-agent` and `promptline-remote`
commands, the desktop entry, icons and man pages. Use `--without-gettext`
if gettext/intltool aren't available (the interface is then English only).
To uninstall:

    python3 setup.py uninstall --manifest=install-files.txt

Promptline installs alongside Terminator: it uses its own command, config
directory (`~/.config/promptline`), D-Bus name and desktop entry. On first
start it copies your Terminator settings.

Terminator's own install notes, which cover its distribution packages, are
in [INSTALL.terminator.md](INSTALL.terminator.md).
