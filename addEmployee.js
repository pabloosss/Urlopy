const { Octokit } = require("@octokit/rest");
const OWNER = "TWOJE-USERNAME";
const REPO  = "TWOJE-REPO";
const PATH  = "baza.json";
const TOKEN = process.env.GH_TOKEN;

exports.handler = async (event) => {
  const octo = new Octokit({ auth: TOKEN });
  const { data } = await octo.repos.getContent({ owner: OWNER, repo: REPO, path: PATH });
  const json = JSON.parse(Buffer.from(data.content, "base64").toString());

  const { imie, dni_urlopowe, manager } = JSON.parse(event.body);
  json.pracownicy.push({ imie, dni_urlopowe, manager });

  await octo.repos.createOrUpdateFileContents({
    owner: OWNER,
    repo: REPO,
    path: PATH,
    message: `Dodano pracownika ${imie}`,
    content: Buffer.from(JSON.stringify(json, null, 2)).toString("base64"),
    sha: data.sha
  });

  return { statusCode: 200, body: "OK" };
};
