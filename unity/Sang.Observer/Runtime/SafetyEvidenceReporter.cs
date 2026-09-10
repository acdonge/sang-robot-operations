using System.Collections;
using System.Text;
using UnityEngine;
using UnityEngine.Networking;

namespace Sang.Observer
{
    public sealed class SafetyEvidenceReporter : MonoBehaviour
    {
        [SerializeField] private MonoBehaviour evidenceSource;
        [SerializeField] private string edgeEndpoint = "http://127.0.0.1:18092/safety/v2/evidence";
        [SerializeField, Min(0.1f)] private float intervalSeconds = 0.2f;

        private ISafetyEvidenceSource Source => evidenceSource as ISafetyEvidenceSource;

        private IEnumerator Start()
        {
            while (enabled)
            {
                if (Source != null && Source.TryCaptureJson(out var json) && !string.IsNullOrWhiteSpace(json))
                {
                    yield return Post(json);
                }
                yield return new WaitForSecondsRealtime(intervalSeconds);
            }
        }

        private IEnumerator Post(string json)
        {
            using var request = new UnityWebRequest(edgeEndpoint, UnityWebRequest.kHttpVerbPOST)
            {
                uploadHandler = new UploadHandlerRaw(Encoding.UTF8.GetBytes(json)),
                downloadHandler = new DownloadHandlerBuffer(),
                timeout = 3
            };
            request.SetRequestHeader("Content-Type", "application/json");
            yield return request.SendWebRequest();
            if (request.result != UnityWebRequest.Result.Success)
            {
                Debug.LogWarning($"SANG observer upload failed: {request.responseCode}");
            }
        }
    }
}
