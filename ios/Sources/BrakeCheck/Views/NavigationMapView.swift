import CoreLocation
import BrakeCheckCore
import MapKit
import SwiftUI
import UIKit

/// The v3 map keeps its smoothing and gesture ownership while displaying all route sources.
struct NavigationMapView: UIViewRepresentable {
    @Environment(\.colorScheme) private var colorScheme
    var route: MKRoute?
    var routePath: MKPolyline?
    var alternatives: [MKRoute] = []
    var stops: [RecentDestination] = []
    var location: CLLocation?
    var locationIsCurrent = true
    var positionIsEstimated = false
    var followsLocation: Bool
    var recenterTrigger: Int
    var topInset: CGFloat = 40
    var isVisible = true
    var satellite = false
    var traffic = true
    var threeDimensional = true
    var headingUp = true
    var nightMode = "system"
    var routeColor = "accent"
    var zoomCommand = 0
    var nextTurnDistance: Double? = nil

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> MKMapView {
        let map = NavigationMapSurface()
        map.delegate = context.coordinator
        map.mapType = .standard
        map.pointOfInterestFilter = .excludingAll
        map.isScrollEnabled = true
        map.isZoomEnabled = true
        map.isRotateEnabled = false
        map.isPitchEnabled = false
        map.showsCompass = false
        map.showsScale = true
        map.showsUserLocation = false
        context.coordinator.attach(map)
        let pinch = UIPinchGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.observePinch(_:)))
        pinch.cancelsTouchesInView = false
        pinch.delegate = context.coordinator
        map.addGestureRecognizer(pinch)
        map.mapInteractionPinch = pinch
        let pan = UIPanGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.observePan(_:)))
        pan.maximumNumberOfTouches = 1
        pan.cancelsTouchesInView = false
        pan.delegate = context.coordinator
        map.addGestureRecognizer(pan)
        map.mapInteractionPan = pan
        return map
    }

    static func dismantleUIView(_ map: MKMapView, coordinator: Coordinator) {
        coordinator.displayLink?.invalidate()
        map.delegate = nil
    }

    func updateUIView(_ map: MKMapView, context: Context) {
        let style: UIUserInterfaceStyle = nightMode == "dark" ? .dark
            : nightMode == "light" ? .light : colorScheme == .dark ? .dark : .light
        let styleChanged = map.overrideUserInterfaceStyle != style
        map.overrideUserInterfaceStyle = style
        let coordinator = context.coordinator
        coordinator.displayLink?.isPaused = !isVisible
        coordinator.locationIsCurrent = locationIsCurrent
        // Service estimates have their own four-second horizon. Never carry one
        // forward through interpolation after the service returns to a cached fix.
        if positionIsEstimated || coordinator.positionIsEstimated != positionIsEstimated {
            coordinator.smoother = NavigationMapSmoother()
            coordinator.lastLocationTimestamp = nil
        }
        coordinator.positionIsEstimated = positionIsEstimated
        coordinator.estimatedAccuracy = location?.horizontalAccuracy ?? 5
        coordinator.cameraPitch = threeDimensional ? 45 : 0
        coordinator.headingUp = headingUp
        if map.mapType != (satellite ? .hybrid : .standard) { map.mapType = satellite ? .hybrid : .standard }
        map.showsTraffic = traffic
        map.showsBuildings = threeDimensional
        let path = routePath ?? route?.polyline
        let insetChanged = coordinator.topInset != topInset
        coordinator.topInset = topInset
        let routeChanged = coordinator.path !== path
        let recenter = coordinator.recenterTrigger != recenterTrigger
        let followChanged = coordinator.wasFollowing != followsLocation
        if recenter {
            coordinator.userPanned = false
            coordinator.manualZoom = false
        }
        let alternativeLines = alternatives.filter { $0 !== route }.map(\.polyline)
        let alternativesChanged = coordinator.alternativeLines.map(ObjectIdentifier.init) != alternativeLines.map(ObjectIdentifier.init)
        if routeChanged || alternativesChanged {
            if let previous = coordinator.path { map.removeOverlay(previous) }
            map.removeOverlays(coordinator.alternativeLines)
            coordinator.path = path
            coordinator.alternativeLines = alternativeLines
            map.addOverlays(alternativeLines, level: .aboveRoads)
            if let path { map.addOverlay(path, level: .aboveRoads) }
        }
        if routeChanged {
            if let destination = coordinator.destination { map.removeAnnotation(destination) }
            coordinator.destination = nil
            if let path, path.pointCount > 0 {
                let destination = MKPointAnnotation()
                destination.coordinate = path.points()[path.pointCount - 1].coordinate
                destination.title = "Destination"
                coordinator.destination = destination
                map.addAnnotation(destination)
            }
        }
        if coordinator.stopIDs != stops.map(\.id) {
            map.removeAnnotations(coordinator.stopAnnotations)
            coordinator.stopIDs = stops.map(\.id)
            coordinator.stopAnnotations = stops.enumerated().map { index, stop in
                let annotation = MKPointAnnotation()
                annotation.coordinate = CLLocationCoordinate2D(latitude: stop.latitude, longitude: stop.longitude)
                annotation.title = stop.title
                annotation.subtitle = "Stop \(index + 1)"
                return annotation
            }
            map.addAnnotations(coordinator.stopAnnotations)
        }
        if styleChanged || coordinator.accentName != BrakeTheme.accentName || coordinator.routeColor != routeColor {
            coordinator.accentName = BrakeTheme.accentName
            coordinator.routeColor = routeColor
            for line in [path].compactMap({ $0 }) + alternativeLines {
                if let renderer = map.renderer(for: line) as? MKPolylineRenderer {
                    renderer.strokeColor = line === path ? coordinator.lineColor(style) : UIColor.systemGray.withAlphaComponent(0.6)
                    renderer.setNeedsDisplay()
                }
            }
            for annotation in [coordinator.destination].compactMap({ $0 }) + coordinator.stopAnnotations {
                (map.view(for: annotation) as? MKMarkerAnnotationView)?.markerTintColor = coordinator.lineColor(style)
            }
        }
        if coordinator.zoomCommand != zoomCommand {
            coordinator.manualZoom = true
            let factor = pow(0.72, Double(zoomCommand - coordinator.zoomCommand))
            let distance = max(180, min(40_000, map.camera.centerCoordinateDistance * factor))
            coordinator.followDistance = min(5_000, distance)
            let camera = map.camera.copy() as! MKMapCamera
            camera.centerCoordinateDistance = distance
            map.setCamera(camera, animated: false)
            coordinator.zoomCommand = zoomCommand
        }
        let validLocation = location.flatMap { CLLocationCoordinate2DIsValid($0.coordinate) ? $0 : nil }
        if let location = validLocation {
            if coordinator.userPosition == nil {
                let annotation = MKPointAnnotation()
                annotation.title = "Your location"
                coordinator.userPosition = annotation
                map.addAnnotation(annotation)
            }
            if coordinator.lastLocationTimestamp != location.timestamp {
                let accepted = coordinator.smoother.ingest(NavigationCoordinate(latitude: location.coordinate.latitude,
                    longitude: location.coordinate.longitude), accuracy: positionIsEstimated ? min(100, max(0, location.horizontalAccuracy)) : location.horizontalAccuracy,
                    speed: location.speed, timestamp: location.timestamp.timeIntervalSince1970,
                    now: Date().timeIntervalSince1970, isCurrent: locationIsCurrent)
                if accepted, locationIsCurrent, location.speed >= 2, location.course >= 0,
                   location.course.isFinite, location.courseAccuracy >= 0, location.courseAccuracy <= 35 {
                    coordinator.targetHeading = location.course
                }
                if let point = coordinator.smoother.coordinate {
                    coordinator.userPosition?.coordinate = CLLocationCoordinate2D(latitude: point.latitude, longitude: point.longitude)
                    coordinator.updateAccuracyCircle(CLLocationCoordinate2D(latitude: point.latitude, longitude: point.longitude))
                }
            }
            coordinator.userPosition?.title = positionIsEstimated ? "Estimated position" : locationIsCurrent ? "Your location" : "Last known GPS location"
            if let annotation = coordinator.userPosition, let view = map.view(for: annotation) {
                let icon = coordinator.positionImage()
                if view.image !== icon { view.image = icon }
                view.accessibilityLabel = positionIsEstimated ? "Estimated position" : locationIsCurrent ? "Approximate GPS location" : "Last known GPS location"
            }
        } else if let annotation = coordinator.userPosition {
            map.removeAnnotation(annotation)
            coordinator.userPosition = nil
            coordinator.smoother = NavigationMapSmoother()
            if let circle = coordinator.accuracyCircle { map.removeOverlay(circle) }
            coordinator.accuracyCircle = nil
        }
        coordinator.following = followsLocation && validLocation != nil
        if coordinator.following {
            coordinator.needsOverviewFit = false
            if followChanged || recenter || !coordinator.hasCentered {
                if !coordinator.manualZoom { coordinator.followDistance = 700 }
                coordinator.followHeading = map.camera.heading
            }
            if coordinator.allowsAutomaticZoom,
               let nextTurnDistance, nextTurnDistance.isFinite, nextTurnDistance >= 0 {
                let measuredSpeed = validLocation?.speed ?? 0
                let speed = measuredSpeed.isFinite ? max(0, measuredSpeed) : 0
                coordinator.followDistance = nextTurnDistance < 80 ? 250
                    : nextTurnDistance < 300 ? 500 : min(1_600, max(700, 700 + speed * 25))
            }
            if !coordinator.hasCentered, let location = validLocation {
                map.setCamera(MKMapCamera(lookingAtCenter: location.coordinate, fromDistance: coordinator.followDistance,
                    pitch: coordinator.cameraPitch, heading: headingUp ? coordinator.targetHeading : 0), animated: false)
            }
            coordinator.hasCentered = true
        } else if !followsLocation && !coordinator.userPanned && (routeChanged || recenter || insetChanged || !coordinator.hasCentered) {
            if path != nil {
                coordinator.needsOverviewFit = true
            } else if let location = validLocation {
                map.setRegion(MKCoordinateRegion(center: location.coordinate, latitudinalMeters: 1800, longitudinalMeters: 1800), animated: coordinator.hasCentered)
                coordinator.hasCentered = true
            }
        }
        coordinator.lastLocationTimestamp = validLocation?.timestamp
        coordinator.wasFollowing = followsLocation
        coordinator.recenterTrigger = recenterTrigger
    }

    @MainActor final class Coordinator: NSObject, @preconcurrency MKMapViewDelegate, UIGestureRecognizerDelegate {
        var userPanned = false
        var accentName = ""
        private var circleBudget = MapOverlayCoalescer()
        private var circleState: String?
        private var positionImages: [String: UIImage] = [:]
        private var positionState: String { positionIsEstimated ? "estimated" : locationIsCurrent ? "current" : "stale" }
        private var positionColor: UIColor { positionIsEstimated ? .systemOrange : locationIsCurrent ? .systemBlue : .systemGray }
        var path: MKPolyline?
        var alternativeLines: [MKPolyline] = []
        var stopIDs: [UUID] = []
        var stopAnnotations: [MKPointAnnotation] = []
        var routeColor = "accent"
        var zoomCommand = 0
        var cameraPitch = 45.0
        var headingUp = true
        var destination: MKPointAnnotation?
        var userPosition: MKPointAnnotation?
        var lastLocationTimestamp: Date?
        var recenterTrigger = 0
        var wasFollowing = false
        var hasCentered = false
        var locationIsCurrent = true
        var positionIsEstimated = false
        var estimatedAccuracy = 5.0
        var topInset: CGFloat = 40
        var smoother = NavigationMapSmoother()
        weak var map: MKMapView?
        var displayLink: CADisplayLink?
        var accuracyCircle: MKCircle?
        var following = false
        var needsOverviewFit = false
        var followDistance = 700.0
        var manualZoom = false
        var followHeading = 0.0
        var targetHeading = 0.0
        private var lastRender = 0.0
        private var lastCircleUpdate = 0.0
        private var applyingCamera = false
        private var userZooming = false
        var allowsAutomaticZoom: Bool { !manualZoom && !userPanned && !userZooming }
        private var lastBounds = CGSize.zero
        private var boundsChangedAt = 0.0

        @MainActor final class LinkTarget: NSObject {
            weak var owner: Coordinator?
            init(_ owner: Coordinator) { self.owner = owner }
            @objc func step(_ link: CADisplayLink) { owner?.render(at: link.timestamp) }
        }
        func attach(_ map: MKMapView) {
            self.map = map
            let link = CADisplayLink(target: LinkTarget(self), selector: #selector(LinkTarget.step(_:)))
            link.preferredFrameRateRange = CAFrameRateRange(minimum: 15, maximum: 30, preferred: 30)
            link.add(to: .main, forMode: .common)
            displayLink = link
        }
        private func render(at time: Double) {
            guard let map, map.window != nil else { lastRender = 0; return }
            let dt = lastRender == 0 ? 1 / 30.0 : min(0.1, time - lastRender)
            lastRender = time
            if map.bounds.size != lastBounds {
                lastBounds = map.bounds.size
                boundsChangedAt = time
                if !following, !userPanned, path != nil { needsOverviewFit = true }
            }
            if needsOverviewFit, !following, !userPanned, time - boundsChangedAt > 0.25,
               map.bounds.height > 20, map.bounds.width > 20, let path {
                needsOverviewFit = false
                map.setVisibleMapRect(path.boundingMapRect,
                    edgePadding: UIEdgeInsets(top: min(topInset, map.bounds.height * 0.4), left: 30, bottom: 44, right: 30),
                    animated: hasCentered)
                hasCentered = true
            }
            guard let point = smoother.advance(seconds: dt) else { return }
            let coordinate = CLLocationCoordinate2D(latitude: point.latitude, longitude: point.longitude)
            if let current = userPosition?.coordinate,
               abs(current.latitude - coordinate.latitude) + abs(current.longitude - coordinate.longitude) > 0.0000005 {
                userPosition?.coordinate = coordinate
            }
            if time - lastCircleUpdate > 0.25 {
                updateAccuracyCircle(coordinate)
                lastCircleUpdate = time
            }
            guard following, !userZooming, !userPanned else { return }
            let factor = 1 - exp(-dt / 0.65)
            let current = map.camera
            let longitudeDelta = (coordinate.longitude - current.centerCoordinate.longitude + 540).truncatingRemainder(dividingBy: 360) - 180
            let center = CLLocationCoordinate2D(latitude: current.centerCoordinate.latitude + (coordinate.latitude - current.centerCoordinate.latitude) * factor,
                longitude: (current.centerCoordinate.longitude + longitudeDelta * factor + 540).truncatingRemainder(dividingBy: 360) - 180)
            let desiredHeading = headingUp ? targetHeading : 0
            let turn = (desiredHeading - followHeading + 540).truncatingRemainder(dividingBy: 360) - 180
            followHeading = (followHeading + turn * factor + 360).truncatingRemainder(dividingBy: 360)
            let distance = current.centerCoordinateDistance + (followDistance - current.centerCoordinateDistance) * factor
            let displacement = MapOverlayCoalescer.distance(
                NavigationCoordinate(latitude: current.centerCoordinate.latitude, longitude: current.centerCoordinate.longitude),
                NavigationCoordinate(latitude: center.latitude, longitude: center.longitude))
            let headingChange = abs((followHeading - current.heading + 540).truncatingRemainder(dividingBy: 360) - 180)
            guard displacement > 0.1 || abs(distance - current.centerCoordinateDistance) > 0.3
                || headingChange > 0.08 || abs(current.pitch - cameraPitch) > 0.03 else { return }
            applyingCamera = true
            map.setCamera(MKMapCamera(lookingAtCenter: center, fromDistance: max(180, distance),
                pitch: current.pitch + (cameraPitch - current.pitch) * factor, heading: followHeading), animated: false)
            applyingCamera = false
        }

        func updateAccuracyCircle(_ center: CLLocationCoordinate2D) {
            guard let map else { return }
            if accuracyCircle == nil { circleBudget.reset() }
            let radius = max(5, positionIsEstimated ? estimatedAccuracy : smoother.uncertaintyRadius)
            let replace = circleBudget.accept(center: NavigationCoordinate(latitude: center.latitude, longitude: center.longitude),
                                               radius: radius)
            if !replace {
                if circleState != positionState, let circle = accuracyCircle,
                   let renderer = map.renderer(for: circle) as? MKCircleRenderer {
                    let color = positionColor
                    renderer.fillColor = color.withAlphaComponent(0.12)
                    renderer.strokeColor = color.withAlphaComponent(0.35)
                    renderer.setNeedsDisplay()
                }
                circleState = positionState
                return
            }
            if let circle = accuracyCircle { map.removeOverlay(circle) }
            let circle = MKCircle(center: center, radius: radius + 5)
            accuracyCircle = circle
            circleState = positionState
            map.addOverlay(circle, level: .aboveRoads)
        }
        @objc func observePan(_ gesture: UIPanGestureRecognizer) {
            if gesture.state == .began || gesture.state == .changed {
                userPanned = true
                manualZoom = true
                needsOverviewFit = false
            }
        }
        @objc func observePinch(_ gesture: UIPinchGestureRecognizer) {
            if gesture.state == .began || gesture.state == .changed {
                userZooming = true
                manualZoom = true
            }
            else {
                if let map { followDistance = max(180, min(5000, map.camera.centerCoordinateDistance)) }
                userZooming = false
            }
        }
        func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer,
            shouldRecognizeSimultaneouslyWith otherGestureRecognizer: UIGestureRecognizer) -> Bool {
            if let scroll = otherGestureRecognizer.view as? UIScrollView, scroll.isPagingEnabled { return false }
            return true
        }
        func positionImage() -> UIImage {
            if let cached = positionImages[positionState] { return cached }
            let icon = UIGraphicsImageRenderer(size: CGSize(width: 26, height: 26)).image { context in
                let color = positionColor
                color.setFill()
                context.cgContext.fillEllipse(in: CGRect(x: 3, y: 3, width: 20, height: 20))
                UIColor.white.setStroke()
                context.cgContext.setLineWidth(3)
                context.cgContext.strokeEllipse(in: CGRect(x: 3, y: 3, width: 20, height: 20))
            }
            positionImages[positionState] = icon
            return icon
        }

        func lineColor(_ style: UIUserInterfaceStyle) -> UIColor {
            if let choice = AccentChoice(rawValue: routeColor) { return choice.color(for: style) }
            return BrakeTheme.accentUIColor(for: style)
        }

        func mapView(_ mapView: MKMapView, rendererFor overlay: MKOverlay) -> MKOverlayRenderer {
            if let circle = overlay as? MKCircle {
                let renderer = MKCircleRenderer(circle: circle)
                let color = positionColor
                renderer.fillColor = color.withAlphaComponent(0.12)
                renderer.strokeColor = color.withAlphaComponent(0.35)
                renderer.lineWidth = 1
                return renderer
            }
            guard let line = overlay as? MKPolyline else { return MKOverlayRenderer(overlay: overlay) }
            let renderer = MKPolylineRenderer(polyline: line)
            let selected = line === path
            renderer.strokeColor = selected ? lineColor(mapView.traitCollection.userInterfaceStyle) : UIColor.systemGray.withAlphaComponent(0.6)
            renderer.lineWidth = selected ? 6 : 4
            renderer.lineCap = .round
            renderer.lineJoin = .round
            return renderer
        }

        func mapView(_ mapView: MKMapView, viewFor annotation: MKAnnotation) -> MKAnnotationView? {
            let isUser = (annotation as AnyObject) === userPosition
            if isUser {
                let view = mapView.dequeueReusableAnnotationView(withIdentifier: "NavigationDot")
                    ?? MKAnnotationView(annotation: annotation, reuseIdentifier: "NavigationDot")
                view.annotation = annotation
                view.image = positionImage()
                view.displayPriority = .required
                view.collisionMode = .circle
                view.accessibilityLabel = positionIsEstimated ? "Estimated position" : locationIsCurrent ? "Approximate GPS location" : "Last known GPS location"
                return view
            }
            let identifier = isUser ? "NavigationPosition" : "NavigationDestination"
            let view = (mapView.dequeueReusableAnnotationView(withIdentifier: identifier) as? MKMarkerAnnotationView)
                ?? MKMarkerAnnotationView(annotation: annotation, reuseIdentifier: identifier)
            view.annotation = annotation
            view.markerTintColor = lineColor(mapView.traitCollection.userInterfaceStyle)
            if let index = stopAnnotations.firstIndex(where: { $0 === annotation }) {
                view.glyphImage = nil
                view.glyphText = "\(index + 1)"
            } else {
                view.glyphText = nil
                view.glyphImage = UIImage(systemName: "flag.fill")
            }
            view.displayPriority = .required
            view.canShowCallout = true
            return view
        }
    }
}

/// Map touches win over the ancestor page scroll view. Touches outside this
/// map (including the destination panel) remain available to the page pager.
private final class NavigationMapSurface: MKMapView {
    var mapInteractionPan: UIPanGestureRecognizer?
    var mapInteractionPinch: UIPinchGestureRecognizer?
    private weak var protectedPager: UIScrollView?
    override func didMoveToWindow() {
        super.didMoveToWindow()
        protectMapGestures()
    }
    override func didMoveToSuperview() {
        super.didMoveToSuperview()
        protectMapGestures()
    }
    private func protectMapGestures() {
        guard let pan = mapInteractionPan else { return }
        var ancestor = superview
        while let view = ancestor {
            if let pager = view as? UIScrollView, pager.isPagingEnabled {
                if protectedPager !== pager {
                    pager.panGestureRecognizer.require(toFail: pan)
                    if let pinch = mapInteractionPinch { pager.panGestureRecognizer.require(toFail: pinch) }
                    protectedPager = pager
                }
                return
            }
            ancestor = view.superview
        }
    }
}
