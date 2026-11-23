import { networkInterfaces } from "node:os";
import { join } from "node:path";

import { PlatformAutomaticEditorialAdapter } from "@wearable/mobile-agent";

import {
  createStudioServer,
  listenStudioServer,
  parseStudioPort,
} from "./studio-server";
import { STUDIO_RELEASE } from "./studio-release";

const port = parseStudioPort(process.env.STUDIO_PORT);
const rootDir = process.env.STUDIO_LOCAL_ROOT ?? join(process.cwd(), ".studio-local");
const hardwareLanEnabled = process.env.STUDIO_HARDWARE_LAN === "1";
const hardwareAddress = hardwareLanEnabled ? requirePrivateIpv4Address() : "127.0.0.1";
const hardwareBaseUrl = process.env.STUDIO_HARDWARE_BASE_URL ?? `http://${hardwareAddress}:${port}`;
const server = createStudioServer({
  rootDir,
  hardwareBaseUrl,
  hardwarePairingEnabled: hardwareLanEnabled,
  editorial: new PlatformAutomaticEditorialAdapter(),
});

await listenStudioServer(server, port, hardwareLanEnabled ? "0.0.0.0" : "127.0.0.1");
process.stdout.write(
  `私人内容工作台 Release ${STUDIO_RELEASE.version} · ${STUDIO_RELEASE.label} · 非生产/未部署: ` +
  `http://127.0.0.1:${port}/studio · ` +
  `${hardwareLanEnabled ? `iPhone 网关 ${hardwareBaseUrl}/hardware/pair · ` : "iPhone 网关未开放 · "}` +
  `本地模型需独立批准，不会自动下载\n`,
);

function requirePrivateIpv4Address(): string {
  const candidates = Object.values(networkInterfaces()).flatMap((entries) => entries ?? [])
    .filter((entry) => entry.family === "IPv4" && !entry.internal)
    .map((entry) => entry.address)
    .filter(isPrivateIpv4)
    .sort((left, right) => privateAddressPriority(left) - privateAddressPriority(right));
  const address = candidates[0];
  if (!address) throw new Error("studio-private-network-unavailable");
  return address;
}

function isPrivateIpv4(address: string): boolean {
  const octets = address.split(".").map(Number);
  return octets.length === 4 && (
    octets[0] === 10 ||
    (octets[0] === 172 && octets[1]! >= 16 && octets[1]! <= 31) ||
    (octets[0] === 192 && octets[1] === 168)
  );
}

function privateAddressPriority(address: string): number {
  if (address.startsWith("192.168.")) return 0;
  if (address.startsWith("10.")) return 1;
  return 2;
}
