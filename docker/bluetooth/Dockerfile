# Both architecture-aware image indexes are pinned. BusyBox supplies a real DNS
# probe; the upstream CoreDNS image intentionally has no shell or probe binary.
FROM coredns/coredns:1.14.7@sha256:7efd3c635b03efd68c4e8398fc45f0d993d0e9ab016f72c1cefb0fd6d01aa286 AS coredns
FROM busybox:1.37.0-musl@sha256:5cec3fc171c87218698e85a52af7087de727372aae264a787b8112901a5b0092
COPY --from=coredns /coredns /coredns
ENTRYPOINT ["/coredns"]
