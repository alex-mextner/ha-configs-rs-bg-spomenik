# Signed Amlogic boot image for ODROID-N2+: BL2 + BL30 + BL31 (Hardkernel/Amlogic blobs from
# LibreELEC/amlogic-boot-fip, directory `odroid-n2-plus`) + mainline U-Boot as BL33.
#
# Must be built on x86_64-linux: Amlogic only ships `aml_encrypt_g12b` as a static x86_64
# binary and provides no source for it.
#
# Output `u-boot.bin.sd.bin` goes to raw sectors of the SD/eMMC: bytes 0..439 (boot code area,
# leaving the MBR disk signature and partition table intact) and from sector 1 onward.
#
# meta.license is deliberately unset: the blobs are redistributable-but-unfree firmware, and
# declaring that would require allowUnfree on the plain legacyPackages set used by the flake.
{
  stdenvNoCC,
  gnumake,
  uboot,
  fipSource,
}:

stdenvNoCC.mkDerivation {
  pname = "uboot-odroid-n2plus-fip";
  inherit (uboot) version;

  src = fipSource;

  nativeBuildInputs = [ gnumake ];

  dontConfigure = true;

  buildPhase = ''
    runHook preBuild
    chmod -R u+w .
    patchShebangs odroid-n2-plus/blx_fix.sh
    chmod +x odroid-n2-plus/aml_encrypt_g12b odroid-n2-plus/blx_fix.sh
    mkdir -p fip-out fip-tmp
    make -C odroid-n2-plus BL33=${uboot}/u-boot.bin O=$PWD/fip-out TMP=$PWD/fip-tmp
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    install -Dm444 -t $out fip-out/u-boot.bin fip-out/u-boot.bin.sd.bin
    runHook postInstall
  '';

  meta = {
    description = "Signed U-Boot boot image (u-boot.bin.sd.bin) for Hardkernel ODROID-N2+";
    platforms = [ "x86_64-linux" ];
  };
}
