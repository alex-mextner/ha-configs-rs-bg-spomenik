# The knobs of this host: everything that is usually changed is an rpi4.* option, set in
# ../configuration.nix.
{ lib, ... }:
{
  options.rpi4.kiosk = {
    urls = lib.mkOption {
      type = lib.types.nonEmptyListOf lib.types.str;
      description = ''
        Kiosk pages. The first one is active, Chromium opens the others in background tabs
        (external code switches them over CDP on 127.0.0.1:9222; the browser has no UI).
      '';
    };

    homeAssistant.url = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "http://odroidn2:8123";
      description = ''
        Home Assistant address out of kiosk.urls. When set, kiosk-ha-login signs in to HA with the
        sops secrets ha-username and ha-password and stores the tokens in that page's localStorage,
        so the kiosk is logged in after every load. null turns the auto-login off.
      '';
    };

    agentTv.url = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "http://192.168.0.10:8855";
      description = ''
        agent-tv dashboard, opened as one more background tab after kiosk.urls. Base URL only:
        the one-time ?token= pairing link is a secret and must not land in the Nix store; open it
        once over CDP, agent-tv then keeps the key in a cookie of the kiosk profile.
      '';
    };
  };
}
