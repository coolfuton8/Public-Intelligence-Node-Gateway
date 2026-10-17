# MCPing stdio bridge for Windows: lets an app that only starts local (stdio) MCP servers, such as the
# Claude desktop app's connectors list, use the MCPing device over HTTPS. Needs nothing beyond the
# Windows PowerShell and .NET Framework that ship with Windows.
#
# The app starts this script; it reads MCP messages from stdin, POSTs each one to the device with the
# access token and writes the replies to stdout. Settings come from environment variables:
#   MCPING_URL    https://mcping.local:8765/mcp
#   MCPING_TOKEN  this computer's access token
#   MCPING_CA     path to the device CA certificate (mcping-ca.crt)
$ErrorActionPreference = 'Stop'
$source = @'
using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Threading;

public static class McpingBridge
{
    static string url, token, session;
    static X509Certificate2 ca;
    static readonly object outLock = new object();
    static StreamWriter stdout;

    static void Send(string json)
    {
        lock (outLock) { stdout.Write(json); stdout.Write("\n"); stdout.Flush(); }
    }

    static bool Debug { get { return !string.IsNullOrEmpty(Environment.GetEnvironmentVariable("MCPING_DEBUG")); } }

    static bool Validate(object sender, X509Certificate cert, X509Chain chain, SslPolicyErrors errors)
    {
        if (Debug) Console.Error.WriteLine("mcping-bridge: tls errors=" + errors + " ca=" + (ca == null ? "none" : ca.Thumbprint));
        if (errors == SslPolicyErrors.None) return true;
        if (ca == null || (errors & ~SslPolicyErrors.RemoteCertificateChainErrors) != 0) return false;
        try
        {
            var c = new X509Chain();
            c.ChainPolicy.ExtraStore.Add(ca);
            c.ChainPolicy.RevocationMode = X509RevocationMode.NoCheck;
            c.ChainPolicy.VerificationFlags = X509VerificationFlags.AllowUnknownCertificateAuthority;
            // Build() is false when any signature in the chain does not verify, so a different CA that
            // merely has the same name cannot vouch for the device.
            if (!c.Build(new X509Certificate2(cert))) return false;
            if (c.ChainElements.Count == 0) return false;
            var root = c.ChainElements[c.ChainElements.Count - 1].Certificate;
            return string.Equals(root.Thumbprint, ca.Thumbprint, StringComparison.OrdinalIgnoreCase);
        }
        catch { return false; }
    }

    static string IdOf(string json)
    {
        // the JSON-RPC id of a request, as raw JSON text, or null for a notification
        int i = json.IndexOf("\"id\"", StringComparison.Ordinal);
        if (i < 0) return null;
        i = json.IndexOf(':', i) + 1;
        while (i < json.Length && char.IsWhiteSpace(json[i])) i++;
        int j = i;
        if (j < json.Length && json[j] == '"') { j++; while (j < json.Length && json[j] != '"') { if (json[j] == '\\') j++; j++; } j++; }
        else while (j < json.Length && json[j] != ',' && json[j] != '}') j++;
        return json.Substring(i, Math.Min(j, json.Length) - i).Trim();
    }

    static void Error(string id, string text)
    {
        if (id == null) { Console.Error.WriteLine("mcping-bridge: " + text); return; }
        Send("{\"jsonrpc\":\"2.0\",\"id\":" + id + ",\"error\":{\"code\":-32000,\"message\":\"" +
             text.Replace("\\", "\\\\").Replace("\"", "'") + "\"}}");
    }

    static void Handle(string body)
    {
        string id = IdOf(body);
        try
        {
            var req = (HttpWebRequest)WebRequest.Create(url);
            req.Method = "POST";
            req.ContentType = "application/json";
            req.Accept = "application/json, text/event-stream";
            req.Headers["Authorization"] = "Bearer " + token;
            if (session != null) req.Headers["Mcp-Session-Id"] = session;
            req.Timeout = 300000;
            req.ReadWriteTimeout = 300000;
            req.ServerCertificateValidationCallback = Validate;
            byte[] data = new UTF8Encoding(false).GetBytes(body);
            using (var s = req.GetRequestStream()) s.Write(data, 0, data.Length);
            using (var resp = (HttpWebResponse)req.GetResponse())
            {
                string sid = resp.Headers["Mcp-Session-Id"];
                if (!string.IsNullOrEmpty(sid)) session = sid;
                using (var r = new StreamReader(resp.GetResponseStream(), Encoding.UTF8))
                {
                    if ((resp.ContentType ?? "").Contains("text/event-stream"))
                    {
                        var buf = new List<string>();
                        string line;
                        while ((line = r.ReadLine()) != null)
                        {
                            if (line.StartsWith("data:")) buf.Add(line.Substring(5).TrimStart());
                            else if (line.Length == 0 && buf.Count > 0) { Send(string.Join("\n", buf)); buf.Clear(); }
                        }
                        if (buf.Count > 0) Send(string.Join("\n", buf));
                    }
                    else
                    {
                        string text = r.ReadToEnd().Trim();
                        if (text.Length > 0) Send(text);
                    }
                }
            }
        }
        catch (WebException ex)
        {
            var hr = ex.Response as HttpWebResponse;
            if (hr != null) Error(id, "device answered HTTP " + (int)hr.StatusCode + (hr.StatusCode == HttpStatusCode.Unauthorized ? " (access token refused)" : ""));
            else Error(id, "cannot reach the device at " + url + ": " + ex.Message);
        }
        catch (Exception ex) { Error(id, "cannot reach the device at " + url + ": " + ex.Message); }
    }

    public static int Run(string u, string t, string caPath)
    {
        url = u; token = t;
        if (!string.IsNullOrEmpty(caPath) && File.Exists(caPath)) ca = new X509Certificate2(caPath);
        ServicePointManager.SecurityProtocol = (SecurityProtocolType)(3072 | 12288);
        stdout = new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false));
        var stdin = new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false));
        var workers = new List<Thread>();
        string line;
        while ((line = stdin.ReadLine()) != null)
        {
            line = line.Trim();
            if (line.Length == 0) continue;
            string body = line;
            var th = new Thread(() => Handle(body)) { IsBackground = true };
            th.Start();
            workers.Add(th);
            workers.RemoveAll(w => !w.IsAlive);
        }
        foreach (var w in workers) w.Join(300000);   // stdin closed: let answers still in flight arrive
        return 0;
    }
}
'@
Add-Type -TypeDefinition $source -ReferencedAssemblies System.dll
$url = if ($env:MCPING_URL) { $env:MCPING_URL } else { 'https://mcping.local:8765/mcp' }
exit ([McpingBridge]::Run($url, $env:MCPING_TOKEN, $env:MCPING_CA))
