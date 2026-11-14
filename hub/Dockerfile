# Apache Jena Fuseki 6.2.0 (RDF 1.2 / SPARQL 1.2) from Maven Central.
FROM eclipse-temurin:21-jre
ARG JENA_VERSION=6.2.0
ADD https://repo1.maven.org/maven2/org/apache/jena/jena-fuseki-server/${JENA_VERSION}/jena-fuseki-server-${JENA_VERSION}.jar /opt/fuseki/fuseki-server.jar
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
EXPOSE 3030
# Read-only hub: one named graph per farm + curated knowledge, rebuilt by hub/build_hub.py.
CMD ["java", "-Xmx2g", "-jar", "/opt/fuseki/fuseki-server.jar", "--file=/data/hub.trig", "--timeout=120000", "--port=3030", "/hub"]
HEALTHCHECK --interval=5s --retries=30 CMD curl -fs http://localhost:3030/$/ping || exit 1
