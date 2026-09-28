# About

This is meant to be a more Linux packaging friendly copy of the AudioProcessing
module from the [ WebRTC ](https://webrtc.googlesource.com/src) project. The
ideal case is that we make no changes to the code to make tracking upstream
code easy.

This package currently only includes the AudioProcessing bits, but I am very
open to collaborating with other projects that wish to distribute other bits of
the code and hopefully eventually have a single point of packaging all the
WebRTC code to help people reuse the code and avoid keeping private copies in
several different projects.

# Building

This project uses the [Meson build system](https://mesonbuild.com/). The
quickest way to build is:

```sh
# Initialise into the build/ directory, for a prefixed install into the
# install/ directory
meson . build -Dprefix=$PWD/install

# Run the actual build
ninja -C build

# Install locally
ninja -C build install

# The libraries, headers, and pkg-config files are now in the install/
# directory
```

# Feedback

Patches, suggestions welcome. You can file an issue on our Gitlab
[repository](https://gitlab.freedesktop.org/pulseaudio/webrtc-audio-processing/).

# Notes

1. It might be nice to try LTO on the library. We build a lot of code as part
   of the main AudioProcessing module deps, and it's possible that this could
   provide significant space savings.
