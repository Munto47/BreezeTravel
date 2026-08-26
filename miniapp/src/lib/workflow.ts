import type {
  ItineraryImportContract,
  TripBriefRevisionContract,
  TripCheckRunContract,
} from '@breezetravel/trip-check-client'

export function unresolvedPlaceIds(itineraryImport: ItineraryImportContract | null): string[] {
  return (itineraryImport?.resolutions || [])
    .filter(item => item.resolution_status === 'AMBIGUOUS' || item.resolution_status === 'NOT_FOUND')
    .map(item => item.raw_stop_id)
}

export function canStartTripCheck(
  brief: TripBriefRevisionContract | null,
  itineraryImport: ItineraryImportContract | null,
  hasRevision: boolean,
): boolean {
  if (brief?.status !== 'CONFIRMED') return false
  if (unresolvedPlaceIds(itineraryImport).length > 0) return false
  return hasRevision || itineraryImport?.status === 'READY'
}

export function canResumeRun(run: TripCheckRunContract | null): boolean {
  return Boolean(run && ['PARTIAL', 'FAILED'].includes(run.status))
}
