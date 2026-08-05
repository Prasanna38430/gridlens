from gridlens import __version__


def test_package_is_importable():
    # this only passes if the package was installed, not if pytest happened
    # to find src/ on sys.path. that is the whole point of the src layout.
    assert __version__
