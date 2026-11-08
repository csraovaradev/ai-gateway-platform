# Keycloak for sso.<domain> on EKS (ADR-0013), with the branded sign-in theme built in.
# Build context: deploy/local/keycloak (where the theme lives):
#   docker build -f deploy/eks/keycloak/Dockerfile deploy/local/keycloak
FROM quay.io/keycloak/keycloak:26.4.0 AS build
# dev-file keeps the demo free of a database; the realm is re-imported from a Secret on every start.
ENV KC_DB=dev-file KC_HEALTH_ENABLED=true
COPY themes/aigw /opt/keycloak/themes/aigw
RUN /opt/keycloak/bin/kc.sh build

FROM quay.io/keycloak/keycloak:26.4.0
COPY --from=build /opt/keycloak/ /opt/keycloak/
ENTRYPOINT ["/opt/keycloak/bin/kc.sh"]
