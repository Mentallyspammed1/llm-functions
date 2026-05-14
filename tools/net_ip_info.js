/**
 * Get network IP information including public IP and network interfaces
 * @typedef {Object} Args
 * @property {boolean} public_ip - Include public IP address (default: true)
 * @property {boolean} interfaces - Include network interface information (default: true)
 * @param {Args} args
 */
const { execSync } = require("child_process");
const os = require("os");

function getPublicIP() {
  try {
    const ipify = execSync("curl -s https://api.ipify.org", { encoding: "utf8" }).trim();
    if (ipify) return ipify;
  } catch (e) {
    console.error("Error fetching public IP from api.ipify.org:", e.message);
  }

  try {
    const ipinfo = execSync("curl -s https://ipinfo.io/ip", { encoding: "utf8" }).trim();
    if (ipinfo) return ipinfo;
  } catch (e) {
    console.error("Error fetching public IP from ipinfo.io:", e.message);
  }

  return "N/A";
}

function getNetworkInterfaces() {
  const interfaces = os.networkInterfaces();
  const result = {};

  for (const [name, addrs] of Object.entries(interfaces)) {
    const validAddrs = addrs
      .filter((addr) => addr.family === "IPv4" && !addr.internal)
      .map((addr) => ({
        address: addr.address,
        netmask: addr.netmask,
        mac: addr.mac,
      }));

    if (validAddrs.length > 0) {
      result[name] = validAddrs;
    }
  }

  return result;
}

exports.run = function (args) {
  const { public_ip = true, interfaces = true } = args;
  const output = {};

  if (public_ip) {
    output.public_ip = getPublicIP();
  }

  if (interfaces) {
    output.interfaces = getNetworkInterfaces();
  }

  return output;
};
