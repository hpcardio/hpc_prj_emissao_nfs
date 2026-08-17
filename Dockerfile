FROM quay.io/astronomer/astro-runtime:13.0.0

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

USER root
WORKDIR /usr/local/airflow

COPY setup_instant_client.sh /tmp/setup_instant_client.sh
RUN chmod +x /tmp/setup_instant_client.sh \
    && /tmp/setup_instant_client.sh \
    && rm /tmp/setup_instant_client.sh

ENV LD_LIBRARY_PATH=/opt/oracle/instantclient_19_23:${LD_LIBRARY_PATH}
ENV PATH=/opt/oracle/instantclient_19_23:${PATH}

COPY requirements.txt pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --no-deps --editable . \
    && python -m playwright install --with-deps chromium

COPY dags ./dags
COPY dbt_glosas_ipm ./dbt_glosas_ipm
COPY include ./include
COPY plugins ./plugins

RUN mkdir -p /usr/local/airflow/data/nfse \
        /usr/local/airflow/data/ipm \
        /usr/local/airflow/data/spu \
        /usr/local/airflow/data/artifacts \
    && chown -R astro:0 /usr/local/airflow

USER astro
