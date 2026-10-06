"use strict";

function required(name) {
    const value = process.env[name];
    if (!value || !value.trim()) {
        throw new Error(`Missing required environment variable: ${name}`);
    }
    return value;
}

const username = required("NODE_RED_ADMIN_USER");
const password = required("NODE_RED_ADMIN_PASSWORD_HASH");
// A malformed hash must never silently disable authentication.
if (!/^\$2[aby]\$(0[4-9]|[12][0-9]|3[01])\$[./A-Za-z0-9]{53}$/.test(password)) {
    throw new Error("NODE_RED_ADMIN_PASSWORD_HASH must be a bcrypt hash");
}
const credentialSecret = required("NODE_RED_CREDENTIAL_SECRET");
if (credentialSecret.length < 32) {
    throw new Error("NODE_RED_CREDENTIAL_SECRET must contain at least 32 characters");
}

module.exports = {
    uiHost: "127.0.0.1",
    uiPort: 1880,
    userDir: "/data",
    flowFile: "flows.json",
    credentialSecret,
    adminAuth: {
        type: "credentials",
        users: [{ username, password, permissions: "*" }],
        sessionExpiryTime: 3600,
    },
    httpNodeAuth: { user: username, pass: password },
    httpStaticAuth: { user: username, pass: password },
};
