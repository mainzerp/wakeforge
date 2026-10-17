// German UI strings, one partial per area of the UI. Keys are the exact English strings passed to t().
import app from "./app";
import exportArea from "./export";
import generate from "./generate";
import mining from "./mining";
import setup from "./setup";
import studio from "./studio";
import train from "./train";
import wakeword from "./wakeword";

const de: Record<string, string> = { ...app, ...generate, ...train, ...wakeword, ...mining, ...studio, ...setup, ...exportArea };
export default de;
