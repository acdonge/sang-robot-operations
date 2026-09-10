namespace Sang.Observer
{
    public interface ISafetyEvidenceSource
    {
        string ProducerId { get; }
        string RunId { get; }
        // JSON must validate as Central SafetyEvidenceIngressV2. Provenance,
        // receive time, and clock measurements are intentionally not producer fields.
        bool TryCaptureJson(out string safetyEvidenceIngressV2Json);
    }
}
