# Home LAN DHCP and DNS: the `home-lan` Compose project in /home/ultra/.local/share/home-lan (release compose file
# from the aisis network bundle + compose.override.yaml + home-lan.env, wrapper `dc`).
#   dhcp       home-lan-dhcp: dnsmasq, host network, UDP 67; config dhcp/dnsmasq-dhcp.conf, leases in
#              HOME_LAN_STATE/dhcp (dnsmasq.leases)
#   dns-relay  home-lan-dns-relay: dns_forwarder.py on 192.168.0.39:53, config private/config.json
#   tls-voice  home-lan-tls-voice: TLS front for the Yandex speakers on 192.168.0.39:443
#
# dnsmasq stays this container instead of services.dnsmasq: HA's host action bridge owns the static leases.
# ha-host-actions.py edits dnsmasq-dhcp.conf, checks it with `docker exec home-lan-dhcp dnsmasq --test` and
# restarts the container through `dc` (DHCP pins from HA). The DNS side is not dnsmasq or CoreDNS at all but
# the relay above; CoreDNS here is only ha-docker-dns, the resolver of HA's Docker network (home-assistant.nix).
{ config, ... }:

let
  dir = "/home/ultra/.local/share/home-lan";
  limits = "/etc/home-lan/compose.limits.yaml";
  compose = "${config.virtualisation.docker.package}/bin/docker compose --project-name home-lan --env-file ${dir}/home-lan.env -f ${dir}/release/compose.network.yaml -f ${dir}/compose.override.yaml -f ${limits} --profile dhcp";
in
{
  # dhcp is profile-gated in compose.override.yaml ("start only via cutover.sh"); it runs here permanently.
  environment.etc."home-lan/compose.limits.yaml".text = ''
    services:
      dhcp:
        deploy: {resources: {limits: {memory: 32m}}}
      dns-relay:
        deploy: {resources: {limits: {memory: 64m}}}
      tls-voice:
        deploy: {resources: {limits: {memory: 64m}}}
  '';

  systemd.services.home-lan = {
    description = "Home LAN DHCP (dnsmasq), DNS relay and Yandex TLS front (Docker Compose)";
    wantedBy = [ "multi-user.target" ];
    # wanted, not required: a rebuild that restarts dockerd must not stop DHCP/DNS (live-restore)
    wants = [
      "docker.service"
      "network-online.target"
    ];
    after = [
      "docker.service"
      "network-online.target"
    ];
    reloadIfChanged = true;
    # with reloadIfChanged a trigger change also becomes a reload, not a restart
    restartTriggers = [ config.environment.etc."home-lan/compose.limits.yaml".text ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      User = "ultra";
      Group = "ultra";
      WorkingDirectory = dir;
      ExecStart = "${compose} up -d dhcp dns-relay tls-voice";
      ExecReload = "${compose} up -d dhcp dns-relay tls-voice";
      ExecStop = "${compose} stop dhcp dns-relay tls-voice";
      TimeoutStartSec = "10min";
      MemoryMax = "128M";
    };
  };
}
