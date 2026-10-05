import catalog from '../../../../backend/app/trip_understanding/data/public-examples.json'

export type HomeExample = {
  id: 'beijing' | 'shenzhen'
  version: number
  city: string
  label: string
  days: string[][]
  text: string
}

// Selecting an example fills editable text; submission creates a new itinerary.
export const HOME_EXAMPLES = catalog as HomeExample[]
