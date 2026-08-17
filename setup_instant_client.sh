#!/bin/sh
set -e

apt-get update

if apt-cache show libaio1 >/dev/null 2>&1; then
	AIO_PACKAGE=libaio1
else
	AIO_PACKAGE=libaio1t64
fi

apt-get install -y --no-install-recommends "$AIO_PACKAGE" wget unzip ca-certificates

if [ ! -e /usr/lib/x86_64-linux-gnu/libaio.so.1 ] && [ -e /usr/lib/x86_64-linux-gnu/libaio.so.1t64 ]; then
	ln -s /usr/lib/x86_64-linux-gnu/libaio.so.1t64 /usr/lib/x86_64-linux-gnu/libaio.so.1
fi

mkdir -p /opt/oracle
cd /opt/oracle

wget -O instantclient.zip \
	https://download.oracle.com/otn_software/linux/instantclient/1923000/instantclient-basiclite-linux.x64-19.23.0.0.0dbru.zip
unzip -q instantclient.zip
rm -f instantclient.zip

cd instantclient_19_23
ln -sf libclntsh.so.19.1 libclntsh.so

if [ -f libocci.so.19.1 ]; then
	ln -sf libocci.so.19.1 libocci.so
fi

echo /opt/oracle/instantclient_19_23 > /etc/ld.so.conf.d/oracle-instantclient.conf
ldconfig

apt-get purge -y --auto-remove wget unzip
rm -rf /var/lib/apt/lists/*
