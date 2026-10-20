import Foundation

public enum NavigationRouteCompositionError: Error, Equatable {
    case invalidLegs, invalidStops, invalidMaximumGap, disconnectedLegs, invalidRoute
}

/// Joins routed legs only when their measured road endpoints meet. A shared POI
/// coordinate does not establish a drivable connection between separate entrances.
public enum NavigationRouteComposition {
    public static func join(legs: [SavedNavigationRoute], title: String,
                            stops: [NavigationRouteStop], preferences: NavigationRoutePreferences,
                            maximumGap: Double = 5) throws -> SavedNavigationRoute {
        guard maximumGap.isFinite, maximumGap >= 0 else {
            throw NavigationRouteCompositionError.invalidMaximumGap
        }
        guard !legs.isEmpty, legs.allSatisfy(\.isValid) else {
            throw NavigationRouteCompositionError.invalidLegs
        }
        // Each intermediate leg ends at a stop. Callers may also retain the final
        // destination as a stop; service previews generally omit that final entry.
        guard (stops.count == legs.count - 1 || stops.count == legs.count), stops.allSatisfy(\.isValid) else {
            throw NavigationRouteCompositionError.invalidStops
        }
        for (previous, next) in zip(legs, legs.dropFirst()) {
            guard previous.coordinates.last!.distance(to: next.coordinates.first!) <= maximumGap else {
                throw NavigationRouteCompositionError.disconnectedLegs
            }
        }
        var coordinates: [NavigationCoordinate] = []
        var routedStops: [NavigationRouteStop] = []
        var distance = 0.0
        var travelTime = 0.0
        for (index, leg) in legs.enumerated() {
            for coordinate in leg.coordinates where coordinates.last.map({ $0.distance(to: coordinate) > 0.01 }) ?? true {
                coordinates.append(coordinate)
            }
            distance += leg.distance
            travelTime += leg.expectedTravelTime
            if stops.indices.contains(index) {
                let stop = stops[index]
                routedStops.append(NavigationRouteStop(id: stop.id, title: stop.title,
                    coordinate: leg.coordinates.last!, dwellTime: stop.dwellTime,
                    routeDistanceFromStart: distance))
            }
        }
        let firstSource = legs[0].source
        let source = legs.allSatisfy { $0.source == firstSource } ? firstSource : "Combined route"
        let route = SavedNavigationRoute(title: title, coordinates: coordinates, steps: legs.flatMap(\.steps),
            distance: distance, expectedTravelTime: travelTime, source: source,
            stops: routedStops, preferences: preferences)
        guard route.isValid else { throw NavigationRouteCompositionError.invalidRoute }
        return route
    }
}
